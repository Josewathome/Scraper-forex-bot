#!/bin/bash
# override_start.sh — ZoneBot container startup
# Runs as the abc user (UID 911) inside the gmag11/metatrader5_vnc container.
# Replaces the default /Metatrader/start.sh via volume mount in docker-compose.yml.

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
# MetaEditor — try both capitalizations since Wine ext4 is case-sensitive
# and the installer may produce MetaEditor64.exe or metaeditor64.exe.
if [ -e '/config/.wine/drive_c/Program Files/MetaTrader 5/MetaEditor64.exe' ]; then
    metaeditor='/config/.wine/drive_c/Program Files/MetaTrader 5/MetaEditor64.exe'
else
    metaeditor='/config/.wine/drive_c/Program Files/MetaTrader 5/metaeditor64.exe'
fi
export WINEPREFIX='/config/.wine'
export WINEDEBUG='-all'
wine_executable="wine"
metatrader_version="5.0.37"
MT5_CMD_OPTIONS="${MT5_CMD_OPTIONS:-}"
mono_url="https://dl.winehq.org/wine/wine-mono/10.3.0/wine-mono-10.3.0-x86.msi"
python_url="https://www.python.org/ftp/python/3.9.13/python-3.9.13.exe"
mt5setup_url="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

# dingmaotu/mql-zmq — the one-stop source for everything ZMQ needs in MT5:
#   - Pre-built Win64 DLLs (libzmq.dll + libsodium.dll) in Library/MT5/
#   - VC2010 variants in Library/VC2010/ (explicitly Wine-compatible per README)
#   - MQL5 Include/Zmq/ wrapper headers
# We download the master branch zip (GitHub codeload — never 404s on active repos).
mql_zmq_url="https://github.com/dingmaotu/mql-zmq/archive/refs/heads/master.zip"

show_message() { echo "$1"; }

check_dependency() {
    if ! command -v "$1" &> /dev/null; then
        echo "$1 is not installed."
        exit 1
    fi
}

is_wine_python_package_installed() {
    $wine_executable python -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
    return $?
}

check_dependency "curl"
check_dependency "$wine_executable"

# unzip is not pre-installed in the base image.
# Install it silently; if apt-get is unavailable fall back to Python's zipfile.
if ! command -v unzip &>/dev/null; then
    apt-get install -y --no-install-recommends unzip 2>/dev/null || true
fi

# ── [0/6] KasmVNC performance fix ─────────────────────────────────
KASMVNC_CFG="${HOME}/.vnc/kasmvnc.yaml"
if [ -f "${KASMVNC_CFG}" ]; then
    if grep -q "rect_threads" "${KASMVNC_CFG}" 2>/dev/null; then
        sed -i 's/rect_threads:.*/rect_threads: 4/' "${KASMVNC_CFG}"
    else
        printf '\nserver:\n  rect_threads: 4\n' >> "${KASMVNC_CFG}"
    fi
    show_message "[0/6] KasmVNC rect_threads set to 4."
else
    show_message "[0/6] kasmvnc.yaml not found — VNC thread patch skipped."
fi

# ── [0/6] Pin Wine MachineGuid ────────────────────────────────────
# IC Markets treats each unique MachineGuid as a new device and triggers
# a mobile authorization request. Wine regenerates this GUID on every
# fresh WINEPREFIX, making every container restart look like a new device.
FIXED_MACHINE_GUID="${WINE_MACHINE_GUID:-a1b2c3d4-e5f6-7890-abcd-ef1234567890}"
CURRENT_GUID=$(wine reg query "HKLM\\SOFTWARE\\Microsoft\\Cryptography" /v MachineGuid 2>/dev/null | grep -i MachineGuid | awk '{print $NF}' || true)
if [ "${CURRENT_GUID}" != "${FIXED_MACHINE_GUID}" ]; then
    show_message "[0/6] Pinning Wine MachineGuid → ${FIXED_MACHINE_GUID} …"
    wine reg add "HKLM\\SOFTWARE\\Microsoft\\Cryptography" \
        /v MachineGuid /t REG_SZ /d "${FIXED_MACHINE_GUID}" /f 2>/dev/null
    show_message "[0/6] MachineGuid pinned."
else
    show_message "[0/6] MachineGuid already pinned."
fi

# ── [1/6] Mono ────────────────────────────────────────────────────
if [ ! -e "/config/.wine/drive_c/windows/mono" ]; then
    show_message "[1/6] Downloading and installing Mono..."
    curl -o /config/.wine/drive_c/mono.msi "$mono_url"
    WINEDLLOVERRIDES=mscoree=d $wine_executable msiexec /i /config/.wine/drive_c/mono.msi /qn
    rm -f /config/.wine/drive_c/mono.msi
    show_message "[1/6] Mono installed."
else
    show_message "[1/6] Mono already installed."
fi

# ── [2/6] MetaTrader 5 ────────────────────────────────────────────
if [ -e "$mt5file" ]; then
    show_message "[2/6] MT5 already installed."
else
    show_message "[2/6] Installing MetaTrader 5..."
    $wine_executable reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    curl -o /config/.wine/drive_c/mt5setup.exe "$mt5setup_url"
    $wine_executable "/config/.wine/drive_c/mt5setup.exe" "/auto"
    sleep 15
    rm -f /config/.wine/drive_c/mt5setup.exe
fi

# ── [3/6] Pre-write MT5 credentials into common.ini ──────────────
MT5_CONFIG_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/Config"
MT5_COMMON_INI="${MT5_CONFIG_DIR}/common.ini"

# Docker Compose interpolates $VAR inside env values, corrupting passwords.
# Re-read MT5_PASSWORD directly from /bot/.env so the raw value is used.
ENV_FILE="/bot/.env"
if [ -f "${ENV_FILE}" ]; then
    _raw_password=$(grep -m1 '^MT5_PASSWORD=' "${ENV_FILE}" | sed 's/^MT5_PASSWORD=//')
    if [ -n "${_raw_password}" ]; then
        MT5_PASSWORD="${_raw_password}"
    fi
fi

if [ -n "${TRADING_ID:-}" ] && [ -n "${MT5_PASSWORD:-}" ] && [ -n "${MT5_SERVER:-}" ]; then
    show_message "[3/6] Writing MT5 credentials to common.ini …"
    show_message "[3/6] TRADING_ID=${TRADING_ID}  SERVER=${MT5_SERVER}  PASSWORD_LEN=$(printf '%s' "${MT5_PASSWORD}" | wc -c)"
    mkdir -p "${MT5_CONFIG_DIR}"
    chmod 644 "${MT5_COMMON_INI}" 2>/dev/null || true

    EXISTING=""
    if [ -f "${MT5_COMMON_INI}" ]; then
        EXISTING=$(awk '
            /^\[Common\]/ { skip=1; next }
            /^\[/ { skip=0 }
            !skip { print }
        ' "${MT5_COMMON_INI}" 2>/dev/null || true)
    fi

    printf '[Common]\r\n'                          >  "${MT5_COMMON_INI}"
    printf 'Login=%s\r\n'   "${TRADING_ID}"        >> "${MT5_COMMON_INI}"
    printf 'Password=%s\r\n' "${MT5_PASSWORD}"     >> "${MT5_COMMON_INI}"
    printf 'Server=%s\r\n'  "${MT5_SERVER}"        >> "${MT5_COMMON_INI}"
    printf 'ProxyEnable=0\r\n'                     >> "${MT5_COMMON_INI}"
    printf 'ProxyType=0\r\n'                       >> "${MT5_COMMON_INI}"
    printf 'ProxyAddress=\r\n'                     >> "${MT5_COMMON_INI}"
    printf 'NewsEnable=1\r\n'                      >> "${MT5_COMMON_INI}"
    if [ -n "${EXISTING}" ]; then
        printf '%s\r\n' "${EXISTING}"              >> "${MT5_COMMON_INI}"
    fi
    show_message "[3/6] common.ini written."
else
    show_message "[3/6] WARNING: credentials not set — manual VNC login required."
fi

# ── [3/6] Account cache — do NOT wipe on restart ─────────────────
# MT5 uses accounts.dat and network.dat to restore the broker session
# on reconnect WITHOUT re-authenticating. Wiping them on every restart
# forces a fresh auth on every container restart, which HFM demo servers
# rate-limit and return "Invalid account" on the 2nd+ attempt.
# We only wipe metaeditor.ini (not a session file — just MetaEditor UI state).
MT5_INSTALL_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5"
if [ -f "${MT5_INSTALL_DIR}/config/metaeditor.ini" ]; then
    rm -f "${MT5_INSTALL_DIR}/config/metaeditor.ini"
fi
show_message "[3/6] Account cache preserved (session files kept for reconnect)."

# ── [3.5/6] ZMQ library installation ─────────────────────────────
# Both the MQL5 EA and the Python bot communicate over ZeroMQ.
# The EA needs:
#   - zmq.dll in MQL5/Libraries/ (the real Win64 libzmq DLL)
#   - Zmq/ folder in MQL5/Include/ (MQL5 wrapper)
# The Python bot needs:
#   - pyzmq installed under Wine Python
#
# WHY zmq.dll lives in MQL5/Libraries/ and NOT the Wine system DLL path:
#   MetaTrader 5 loads DLLs imported by EAs from MQL5/Libraries/ first,
#   then falls back to Windows system paths. Placing it here avoids
#   any Wine DLL override complexity and keeps MT5 self-contained.
#   The Python pyzmq package bundles its own libzmq — no shared object.

MT5_MQL5_DIR="${MT5_INSTALL_DIR}/MQL5"
MT5_LIBS_DIR="${MT5_MQL5_DIR}/Libraries"
MT5_INC_DIR="${MT5_MQL5_DIR}/Include"
MT5_EXPERTS_DIR="${MT5_MQL5_DIR}/Experts"
ZMQ_MARKER="/config/.zmq_installed"

mkdir -p "${MT5_LIBS_DIR}" "${MT5_INC_DIR}" "${MT5_EXPERTS_DIR}"

# Invalidate marker if any of:
#   a) The ZMQ include headers are missing
#   b) Zmq.mqh is still UTF-16 encoded (MetaEditor needs UTF-8 — UTF-16 causes 226 errors)
#   c) The mql-lang dependency (Mql/) is missing (causes 226 compile errors in Socket.mqh)
if [ ! -d "${MT5_INC_DIR}/Zmq" ]; then
    rm -f "${ZMQ_MARKER}"
elif [ ! -f "${MT5_INC_DIR}/Mql/Lang/Mql.mqh" ]; then
    show_message "[3.5/6] mql4-lib missing — invalidating marker to install dependency."
    rm -f "${ZMQ_MARKER}"
elif python3 -c "
import sys
d = open('${MT5_INC_DIR}/Zmq/Zmq.mqh','rb').read(2)
sys.exit(0 if d in (b'\xff\xfe', b'\xfe\xff') else 1)
" 2>/dev/null; then
    show_message "[3.5/6] Zmq.mqh is UTF-16 — invalidating marker to fix encoding."
    rm -f "${ZMQ_MARKER}"
fi

if [ ! -f "${ZMQ_MARKER}" ]; then
    show_message "[3.5/6] Installing ZeroMQ for MQL5 from dingmaotu/mql-zmq..."
    _zmq_tmp="/tmp/zmq_install"
    mkdir -p "${_zmq_tmp}"

    # dingmaotu/mql-zmq is the canonical MQL5 ZMQ distribution.
    # It ships everything in one archive:
    #   Include/Zmq/     — MQL5 wrapper headers (what ZoneBotBridge.mq5 includes)
    #   Library/MT5/     — libzmq.dll + libsodium.dll (Win64, standard build)
    #   Library/VC2010/  — libzmq.dll + libsodium.dll (Win64, VC2010 runtime — Wine-compatible)
    # We use the VC2010 variants because they depend on msvcr100.dll which Wine
    # ships by default, whereas newer MSVC runtimes require manual Wine DLL overrides.

    if curl -fL --retry 3 --retry-delay 5 \
            "${mql_zmq_url}" \
            -o "${_zmq_tmp}/mql-zmq.zip"; then

        # Extract — prefer unzip, fall back to Python's zipfile (always present)
        mkdir -p "${_zmq_tmp}/mql-zmq"
        if command -v unzip &>/dev/null; then
            unzip -q "${_zmq_tmp}/mql-zmq.zip" -d "${_zmq_tmp}/mql-zmq" 2>/dev/null || true
        else
            python3 -c "
import zipfile
with zipfile.ZipFile('${_zmq_tmp}/mql-zmq.zip') as z:
    z.extractall('${_zmq_tmp}/mql-zmq')
" 2>/dev/null || true
        fi

        # GitHub zips have a top-level directory (mql-zmq-master/) — strip it
        _repo_root=$(find "${_zmq_tmp}/mql-zmq" -maxdepth 1 -mindepth 1 -type d | head -1)
        if [ -z "${_repo_root}" ]; then
            _repo_root="${_zmq_tmp}/mql-zmq"
        fi

        # ── Install MQL5 headers ─────────────────────────────────
        if [ -d "${_repo_root}/Include/Zmq" ]; then
            rm -rf "${MT5_INC_DIR}/Zmq"
            cp -r "${_repo_root}/Include/Zmq" "${MT5_INC_DIR}/Zmq"
            # Some files in the archive are UTF-16 encoded (e.g. Zmq.mqh).
            # MetaEditor requires UTF-8 or ANSI — UTF-16 produces 226+ errors.
            # Convert any UTF-16 .mqh files to UTF-8 in place.
            python3 -c "
import os
inc = '${MT5_INC_DIR}/Zmq'
for fname in os.listdir(inc):
    if not fname.endswith('.mqh'): continue
    path = os.path.join(inc, fname)
    data = open(path, 'rb').read()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = data.decode('utf-16', errors='replace')
        open(path, 'w', encoding='utf-8').write(text)
        print('  Converted ' + fname + ' from UTF-16 to UTF-8')
" 2>/dev/null || true
            show_message "[3.5/6] Include/Zmq/ installed to MQL5/Include/"
        else
            show_message "[3.5/6] WARNING: Include/Zmq/ not found in archive — EA will not compile"
        fi

        # ── Install mql4-lib dependency ───────────────────────────
        # mql-zmq's Socket.mqh includes <Mql/Lang/Mql.mqh> and
        # <Mql/Lang/Native.mqh> from dingmaotu/mql4-lib.
        # Without these, every class/method reference fails → 226 errors.
        # Archive layout: Lang/Mql.mqh → install at MQL5/Include/Mql/Lang/Mql.mqh
        if [ ! -f "${MT5_INC_DIR}/Mql/Lang/Mql.mqh" ]; then
            show_message "[3.5/6] Downloading mql4-lib dependency..."
            _mql_tmp="/tmp/mql4_lang"
            mkdir -p "${_mql_tmp}"
            if curl -fL --retry 3 --retry-delay 5 \
                    "https://github.com/dingmaotu/mql4-lib/archive/refs/heads/master.zip" \
                    -o "${_mql_tmp}/mql4.zip" 2>/dev/null; then
                python3 -c "import zipfile; zipfile.ZipFile('${_mql_tmp}/mql4.zip').extractall('${_mql_tmp}')" 2>/dev/null || true
                _lang_root=$(find "${_mql_tmp}" -maxdepth 1 -mindepth 1 -type d | head -1)
                if [ -d "${_lang_root}/Lang" ]; then
                    mkdir -p "${MT5_INC_DIR}/Mql/Lang"
                    cp "${_lang_root}/Lang/"*.mqh "${MT5_INC_DIR}/Mql/Lang/" 2>/dev/null || true
                    show_message "[3.5/6] mql4-lib Mql/Lang/ installed ($(ls "${MT5_INC_DIR}/Mql/Lang/" | wc -l) files)."
                else
                    show_message "[3.5/6] WARNING: mql4-lib Lang/ not found in archive."
                fi
            else
                show_message "[3.5/6] WARNING: mql4-lib download failed."
            fi
            rm -rf "${_mql_tmp}"
        else
            show_message "[3.5/6] mql4-lib already installed."
        fi

        # ── Install DLLs ─────────────────────────────────────────
        # dingmaotu/mql-zmq archive layout:
        #   Library/VC2010/x64/  — Win64 VC2010 DLLs (Wine-compatible, preferred)
        #   Library/VC2010/x86/  — Win32 VC2010 DLLs (skip — MT5 is 64-bit)
        #   Library/MT5/         — Win64 standard MSVC (fallback)
        # We need x64 DLLs because MetaTrader 5 is a 64-bit process.
        _dll_src=""
        if [ -d "${_repo_root}/Library/VC2010/x64" ]; then
            _dll_src="${_repo_root}/Library/VC2010/x64"
            show_message "[3.5/6] Using VC2010/x64 DLLs (Wine-compatible)"
        elif [ -d "${_repo_root}/Library/MT5" ]; then
            _dll_src="${_repo_root}/Library/MT5"
            show_message "[3.5/6] Using MT5/ DLLs (VC2010/x64 not found)"
        fi

        if [ -n "${_dll_src}" ]; then
            # Copy all DLLs from the chosen directory into MQL5/Libraries/.
            # Use find instead of glob — glob silently fails on paths with spaces.
            _dll_count=0
            while IFS= read -r -d '' _dll; do
                cp "${_dll}" "${MT5_LIBS_DIR}/"
                show_message "[3.5/6] Installed: $(basename "${_dll}")"
                _dll_count=$((_dll_count + 1))
            done < <(find "${_dll_src}" -maxdepth 1 -name "*.dll" -print0 2>/dev/null)
            if [ "${_dll_count}" -eq 0 ]; then
                show_message "[3.5/6] WARNING: no .dll files found in ${_dll_src}"
                show_message "[3.5/6] Contents: $(ls "${_dll_src}" 2>/dev/null | tr '\n' ' ')"
            fi
        else
            show_message "[3.5/6] WARNING: no Library/VC2010/x64 or Library/MT5 found — DLLs missing"
        fi

        rm -rf "${_zmq_tmp}"
        # Only mark success if headers were actually installed
        if [ -d "${MT5_INC_DIR}/Zmq" ]; then
            touch "${ZMQ_MARKER}"
            show_message "[3.5/6] ZMQ library installation complete."
        else
            show_message "[3.5/6] WARNING: ZMQ install incomplete — will retry on next restart."
        fi
    else
        show_message "[3.5/6] WARNING: mql-zmq download failed — will retry on next restart."
        show_message "[3.5/6] ZMQ EA disabled for this session; attach manually via VNC."
        rm -rf "${_zmq_tmp}"
        # Do NOT touch ZMQ_MARKER — retry on next restart
    fi
else
    show_message "[3.5/6] ZMQ library already installed."
fi

# ── Pre-launch: Inject EA into GBPUSD H1 chart file ──────────────
# This runs BEFORE MT5 starts so the .chr file is ready when MT5
# loads its profile. MT5 reads chart files at startup and will
# attach ZoneBotBridge automatically — no VNC or AutoTrade.ini needed.
_symbols_pre="${SYMBOLS_CSV:-GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF}"
_chr_dir_pre="${MT5_INSTALL_DIR}/Profiles/Charts/Default"
mkdir -p "${_chr_dir_pre}"
SYMBOLS_CSV="${_symbols_pre}" python3 - <<'PRELAUNCH_PYEOF'
import os, sys, glob

mt5_dir  = "/config/.wine/drive_c/Program Files/MetaTrader 5"
chr_dir  = os.path.join(mt5_dir, "Profiles", "Charts", "Default")
symbols  = os.environ.get("SYMBOLS_CSV", "GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF")
ea_name  = "ZoneBotBridge"
endpoint = "tcp://127.0.0.1:5556"

EXPERT_BLOCK = "\n<expert>\nname={ea}\nflags=3\nwindow=0\n\n<inputs>\nPUB_ENDPOINT={ep}\nSYMBOLS_CSV={sym}\nHEARTBEAT_SECS=5\n</inputs>\n\n</expert>\n".format(
    ea=ea_name, ep=endpoint, sym=symbols)

os.makedirs(chr_dir, exist_ok=True)
chrs = sorted(glob.glob(os.path.join(chr_dir, "*.chr")))

# Find GBPUSD H1 chart (period_type=2 period_size=1 in MT5 = H1)
target = None
for path in chrs:
    try:
        raw  = open(path, "rb").read()
        text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")
        if "symbol=GBPUSD" in text and "period_type=2" in text and "period_size=1" in text:
            target = path
            break
    except Exception:
        pass

# Fallback: any GBPUSD chart
if target is None:
    for path in chrs:
        try:
            raw  = open(path, "rb").read()
            text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")
            if "symbol=GBPUSD" in text:
                target = path
                break
        except Exception:
            pass

# Still nothing — create a minimal GBPUSD H1 chart
if target is None:
    idx = len(chrs) + 1
    target = os.path.join(chr_dir, "chart{:02d}.chr".format(idx))
    minimal = "<chart>\nsymbol=GBPUSD\nperiod_type=2\nperiod_size=1\ndigits=5\ntick_size=0.000000\nscale=4\nmode=1\nbidline=1\n\n<window>\nheight=100\n\n<indicator>\nname=Main\npath=\napply=1\nshow_data=1\nscale_inherit=0\nscale_line=0\nscale_line_percent=50\nscale_line_value=0.000000\nscale_fix_min=0\nscale_fix_min_val=0.000000\nscale_fix_max=0\nscale_fix_max_val=0.000000\n</indicator>\n\n</window>\n\n</chart>\n"
    open(target, "wb").write(b"\xff\xfe" + minimal.encode("utf-16-le"))
    print("Created new GBPUSD H1 chart:", target)

raw  = open(target, "rb").read()
text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")

if ea_name in text:
    print("EA already injected in:", os.path.basename(target))
    sys.exit(0)

if "</chart>" in text:
    text = text.replace("</chart>", EXPERT_BLOCK + "</chart>", 1)
else:
    text = text.rstrip() + EXPERT_BLOCK + "\n</chart>\n"

open(target, "wb").write(b"\xff\xfe" + text.encode("utf-16-le"))
print("EA injected into:", os.path.basename(target))
PRELAUNCH_PYEOF
show_message "Pre-launch chart injection done."

# ── [3/6] Launch MT5 terminal ─────────────────────────────────────
if [ -e "$mt5file" ]; then
    show_message "[3/6] Launching MT5 terminal..."
    $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
    show_message "[3/6] MT5 launched (PID $!)."
else
    show_message "[3/6] ERROR: MT5 binary not found — cannot continue."
    exit 1
fi

# ── [4/6] Wine Python ─────────────────────────────────────────────
if ! $wine_executable python --version 2>/dev/null; then
    show_message "[4/6] Installing Python 3.9 in Wine..."
    curl -L "$python_url" -o /tmp/python-installer.exe
    $wine_executable /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    rm /tmp/python-installer.exe
    show_message "[4/6] Python installed."
else
    show_message "[4/6] Wine Python already installed."
fi

# ── [5/6] MetaTrader5 Python library ─────────────────────────────
show_message "[5/6] Checking Python packages..."
$wine_executable python -m pip install --upgrade --no-cache-dir pip --quiet

if ! is_wine_python_package_installed "MetaTrader5==$metatrader_version"; then
    show_message "[5/6] Installing MetaTrader5==$metatrader_version..."
    $wine_executable python -m pip install --no-cache-dir "MetaTrader5==$metatrader_version"
fi

show_message "[5/6] Pinning numpy<2 for stability..."
$wine_executable python -m pip install --no-cache-dir "numpy<2" --quiet

# ── [5.5/6] pyzmq for Wine Python ────────────────────────────────
# pyzmq must be installed inside Wine Python (not the Linux system Python)
# because the bot runs as `wine python`. pyzmq bundles its own libzmq
# shared library — it does NOT use the zmq.dll we installed for the MQL5 EA.
# The two ZMQ instances (EA side / Python side) are completely independent
# C libraries that communicate over Wine's loopback TCP stack.
show_message "[5.5/6] Installing pyzmq in Wine Python..."
if ! is_wine_python_package_installed "pyzmq"; then
    # Pin to zmq 24.x — compatible with Python 3.9 and the bundled libzmq 4.3.x
    $wine_executable python -m pip install --no-cache-dir "pyzmq>=24,<26" --quiet
    show_message "[5.5/6] pyzmq installed."
else
    show_message "[5.5/6] pyzmq already installed."
fi

# ── [6/6] Bot dependencies ────────────────────────────────────────
show_message "[6/6] Installing bot dependencies in Wine Python..."
$wine_executable python -m pip install --no-cache-dir --quiet requests tzdata flask

if [ -f /bot/src/requirements.txt ]; then
    show_message "[6/6] Installing src/requirements.txt in Wine Python..."
    $wine_executable python -m pip install --no-cache-dir -r /bot/src/requirements.txt
fi

# ── WAIT FOR OPERATOR CONFIRMATION (first run only) ───────────────
# /config is a persistent Docker volume — ready_to_trade survives
# container restarts and power cuts so the bot resumes automatically.
# Only missing on the very first run, or after:
#   docker exec scalper-prime rm /config/ready_to_trade  (manual reset)
if [ ! -f /config/ready_to_trade ]; then
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  Scalper Bot — first-run setup required"
    echo "════════════════════════════════════════════════════════"
    echo ""
    echo "  1. Open http://localhost:3001 — confirm MT5 is logged in"
    echo "     and charts are loaded"
    echo "  2. Tools → Options → Expert Advisors"
    echo "     → Allow Algorithmic Trading"
    echo "  3. Run this command to start the bot:"
    echo "     docker exec scalper-prime touch /config/ready_to_trade"
    echo ""
    echo "  Waiting for ready_to_trade signal..."
    while [ ! -f /config/ready_to_trade ]; do
        sleep 5
    done
    echo "  ready_to_trade received — starting bot."
else
    echo "  ready_to_trade present — resuming bot immediately."
fi

# ── Deploy and compile ZoneBotBridge EA ──────────────────────────
EA_SRC="/bot/src/infrastructure/mt5_bridge/ea/ZoneBotBridge.mq5"
EA_DST="${MT5_EXPERTS_DIR}/ZoneBotBridge.mq5"
EA_EX5="${MT5_EXPERTS_DIR}/ZoneBotBridge.ex5"
AUTO_TRADE_INI="${MT5_CONFIG_DIR}/AutoTrade.ini"

# Ensure directories exist (MT5 should have created them, but be safe)
mkdir -p "${MT5_EXPERTS_DIR}" "${MT5_CONFIG_DIR}"

# Re-copy ZMQ DLLs now that MQL5/Libraries/ is confirmed to exist
if [ -f "${MT5_LIBS_DIR}/libzmq.dll" ] || [ -f "${MT5_LIBS_DIR}/zmq.dll" ]; then
    show_message "ZMQ DLLs already in MQL5/Libraries/."
else
    show_message "WARNING: ZMQ DLLs missing from MQL5/Libraries/ — EA will fail to compile."
    show_message "Run: docker compose restart   to trigger re-download."
fi

if [ -f "${EA_SRC}" ]; then
    cp "${EA_SRC}" "${EA_DST}"
    show_message "ZoneBotBridge.mq5 deployed to MQL5/Experts/"

    # Compile with MetaEditor if available.
    # MetaEditor needs MT5 to be fully authorized and MQL5 environment loaded
    # before it can compile — if we run it too early the compile silently
    # produces nothing.  Poll the MT5 log for "terminal synchronized" (which
    # only appears AFTER the broker handshake is complete) before compiling.
    if [ -e "$metaeditor" ]; then
        MT5_LOG_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/logs"
        _compile_wait=0
        _compile_timeout=120
        show_message "Waiting for MT5 to synchronize before compiling EA (up to ${_compile_timeout}s)..."
        while [ ${_compile_wait} -lt ${_compile_timeout} ]; do
            _latest_log=$(ls -t "${MT5_LOG_DIR}"/*.log 2>/dev/null | head -1)
            if [ -n "${_latest_log}" ]; then
                _sync_line=$(python3 -c "
import sys
data = open('${_latest_log}','rb').read().decode('utf-16-le',errors='replace')
for line in data.splitlines():
    if 'terminal synchronized' in line.lower():
        print(line.strip())
" 2>/dev/null | tail -1)
                if [ -n "${_sync_line}" ]; then
                    show_message "MT5 synchronized — compiling EA now."
                    break
                fi
            fi
            sleep 5
            _compile_wait=$((_compile_wait + 5))
        done
        if [ ${_compile_wait} -ge ${_compile_timeout} ]; then
            show_message "MT5 sync wait timed out — attempting compile anyway."
        fi

        show_message "Compiling ZoneBotBridge.mq5 ..."
        # CRITICAL: pass a relative path to /compile: — absolute paths under Wine
        # break #include resolution (Wine strips the leading slash from include lookups).
        # Fix: cd into the MQL5 root so MetaEditor resolves includes from there.
        # The log is written next to the source file (Experts/ZoneBotBridge.log)
        # when no absolute log path is given.
        (
            cd "/config/.wine/drive_c/Program Files/MetaTrader 5" && \
            DISPLAY=:1 WINEPREFIX=/config/.wine timeout 120 \
                $wine_executable "$metaeditor" \
                "/compile:MQL5\\Experts\\ZoneBotBridge.mq5" \
                "/log:MQL5\\Experts\\ZoneBotBridge.log" \
                2>/dev/null || true
        )
        sleep 5
        _compile_log="${MT5_EXPERTS_DIR}/ZoneBotBridge.log"
        if [ -f "${EA_EX5}" ]; then
            show_message "ZoneBotBridge.ex5 compiled successfully."
        else
            show_message "WARNING: ZoneBotBridge.ex5 not produced."
            if [ -f "${_compile_log}" ]; then
                show_message "Compile errors:"
                python3 -c "
data = open('${_compile_log}','rb').read()
# UTF-16 LE log
try:
    text = data.decode('utf-16-le')
    if text.startswith('﻿'): text = text[1:]
except:
    text = data.decode('utf-8', errors='replace')
for line in text.splitlines():
    line = line.strip()
    if line:
        print(line)
" 2>/dev/null | head -30 || true
            fi
        fi
    else
        show_message "WARNING: MetaEditor not found at: $metaeditor"
        ls "/config/.wine/drive_c/Program Files/MetaTrader 5/" 2>/dev/null || true
    fi
else
    show_message "WARNING: EA source not found at ${EA_SRC}"
fi

# ── Inject EA into GBPUSD H1 chart file (reliable auto-load) ──────
# AutoTrade.ini only fires when there is no saved chart state — it is
# ignored if a .chr file already exists for that chart.  Instead we
# write the <expert> block directly into the GBPUSD H1 .chr file so
# MT5 loads ZoneBotBridge on EVERY restart, no VNC needed.
#
# MT5 .chr files are UTF-16 LE with BOM. We locate the GBPUSD H1
# chart (or any GBPUSD chart as fallback), then insert the <expert>
# block right before the closing </chart> tag if it isn't there yet.
# MT5 must be stopped while we edit — it is still starting at this
# point (launched above in the background), so we wait for the first
# sync then edit and let MT5 re-read on the next start. But since we
# ALSO restart MT5 after this via the bot loop's own restart cycle,
# the simplest approach is: edit now (MT5 is mid-startup and hasn't
# locked the .chr files yet), then MT5 will read them fresh.
#
# Profile search order: Profiles/Charts/Default/  (the active profile)
if [ -f "${EA_EX5}" ]; then
    _symbols="${SYMBOLS_CSV:-GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF}"
    _chr_dir="${MT5_INSTALL_DIR}/Profiles/Charts/Default"
    mkdir -p "${_chr_dir}"

    # Pass _symbols into the Python heredoc via the environment.
    # The pre-launch block used SYMBOLS_CSV=... prefix; we do the same here so
    # both injection passes use the same symbol list from the .env file.
    _chart_inject_result=0
    SYMBOLS_CSV="${_symbols}" python3 - <<'PYEOF'
import os, sys, glob

mt5_dir  = "/config/.wine/drive_c/Program Files/MetaTrader 5"
chr_dir  = os.path.join(mt5_dir, "Profiles", "Charts", "Default")
symbols  = os.environ.get("SYMBOLS_CSV", "GBPUSD,XAUUSD,USDJPY,AUDUSD,USDCHF")
ea_name  = "ZoneBotBridge"
endpoint = "tcp://127.0.0.1:5556"

# The <expert> XML block MT5 uses to persist an attached EA.
# period_type=2 period_size=1 → H1 (type 2 = hours, size 1)
EXPERT_BLOCK = """\n<expert>\nname={ea}\nflags=3\nwindow=0\n\n<inputs>\nPUB_ENDPOINT={ep}\nSYMBOLS_CSV={sym}\nHEARTBEAT_SECS=5\n</inputs>\n\n</expert>\n""".format(
    ea=ea_name, ep=endpoint, sym=symbols)

os.makedirs(chr_dir, exist_ok=True)
chrs = sorted(glob.glob(os.path.join(chr_dir, "*.chr")))

# Find existing GBPUSD H1 chart or pick a GBPUSD chart
target = None
for path in chrs:
    try:
        raw  = open(path, "rb").read()
        text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")
        if "symbol=GBPUSD" in text and "period_type=2" in text and "period_size=1" in text:
            target = path
            break
    except Exception:
        pass

# Fallback: any GBPUSD chart
if target is None:
    for path in chrs:
        try:
            raw  = open(path, "rb").read()
            text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")
            if "symbol=GBPUSD" in text:
                target = path
                break
        except Exception:
            pass

# Still nothing — create a minimal GBPUSD H1 chart
if target is None:
    idx = len(chrs) + 1
    target = os.path.join(chr_dir, "chart{:02d}.chr".format(idx))
    minimal = "<chart>\nsymbol=GBPUSD\nperiod_type=2\nperiod_size=1\ndigits=5\ntick_size=0.000000\nscale=4\nmode=1\nbidline=1\n\n<window>\nheight=100\n\n<indicator>\nname=Main\npath=\napply=1\nshow_data=1\nscale_inherit=0\nscale_line=0\nscale_line_percent=50\nscale_line_value=0.000000\nscale_fix_min=0\nscale_fix_min_val=0.000000\nscale_fix_max=0\nscale_fix_max_val=0.000000\n</indicator>\n\n</window>\n\n</chart>\n"
    open(target, "wb").write(b"\xff\xfe" + minimal.encode("utf-16-le"))
    print("Created new GBPUSD H1 chart:", target)

# Read, check, patch
raw  = open(target, "rb").read()
text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")

if ea_name in text:
    print("EA already in chart file:", target)
    # Exit 2 = already present, no restart needed
    sys.exit(2)

# Insert <expert> block before </chart>
if "</chart>" in text:
    text = text.replace("</chart>", EXPERT_BLOCK + "</chart>", 1)
else:
    text = text.rstrip() + EXPERT_BLOCK + "\n</chart>\n"

# Write UTF-16-LE with BOM.  Prepend the BOM character as the first code-unit
# so the file starts with the two-byte sequence FF FE.
open(target, "wb").write(b"\xff\xfe" + text.encode("utf-16-le"))
print("EA injected into chart file:", target)
# Exit 0 = freshly injected, MT5 restart required
sys.exit(0)
PYEOF
    _chart_inject_result=$?
    show_message "EA chart injection complete (exit=${_chart_inject_result})."

    # Also write AutoTrade.ini as belt-and-suspenders for fresh installs
    # (when no .chr files exist yet, AutoTrade.ini fires on first MT5 launch).
    if ! grep -q "ZoneBotBridge" "${AUTO_TRADE_INI}" 2>/dev/null; then
        printf '[Expert]\r\nName=ZoneBotBridge\r\nSymbol=GBPUSD\r\nPeriod=H1\r\n' > "${AUTO_TRADE_INI}"
        printf 'Parameters=PUB_ENDPOINT=tcp://127.0.0.1:5556;SYMBOLS_CSV=%s;HEARTBEAT_SECS=5\r\n' \
               "${_symbols}" >> "${AUTO_TRADE_INI}"
        show_message "AutoTrade.ini written as fallback for fresh installs."
    fi

    # Restart MT5 ONLY when the chart was freshly modified (exit 0).
    # When the EA was already in the chart (exit 2) MT5 already has it loaded —
    # restarting would waste 25 s and briefly drop the broker connection.
    if [ "${_chart_inject_result}" -eq 0 ]; then
        show_message "Chart modified — restarting MT5 so it loads the injected EA..."
        # taskkill is the Windows-native way to stop a process by name inside Wine.
        # || true: if MT5 already exited for any reason, this is harmless.
        $wine_executable taskkill /IM terminal64.exe /F 2>/dev/null || true
        sleep 5
        # Relaunch MT5 in the background (same way as the initial launch above).
        $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
        show_message "MT5 restarted — waiting 20s for EA to bind ZMQ PUB socket..."
        sleep 20
    else
        show_message "EA already present in chart — MT5 restart skipped."
    fi
fi

# ── BOT DATA DIRECTORIES ──────────────────────────────────────────
# Create all directories the bot writes to before Wine Python starts.
# Wine Python runs as a Windows process and cannot create Linux directories
# with the right permissions — doing it here (as root in the Linux layer)
# ensures the paths exist and are writable by UID 911 (the container user).
#
# These mirror the paths used in the Python source:
#   analytics/   — tick velocity analytics (tick_analytics.py)
#   .checkpoints/ — restart checkpoints     (checkpoint_service.py)
#   logs/         — trading log files       (logging config)
#
BOT_DIR="/bot"
for _dir in \
    "${BOT_DIR}/analytics" \
    "${BOT_DIR}/.checkpoints" \
    "${BOT_DIR}/logs"
do
    if [ ! -d "${_dir}" ]; then
        mkdir -p "${_dir}"
        chmod 777 "${_dir}"
        echo "Created bot data directory: ${_dir}"
    fi
done

# ── START BOT ─────────────────────────────────────────────────────
# Launch the event-driven streaming bot (main_stream.py).
# Wine reports os.name == "nt" so mt5_gateway.py uses direct
# MetaTrader5 import — no RPyC bridge.
# PYTHONUTF8=1 forces UTF-8 to prevent cp1252 crashes on box-drawing chars.
echo "Starting ZoneBot (stream mode) in Wine Python..."
cd /bot && DISPLAY=:1 WINEPREFIX=/config/.wine PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
    $wine_executable python -m src.main_stream
