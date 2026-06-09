#!/bin/bash
# override_start.sh — ZoneBot container startup
# Runs as the abc user (UID 911) inside the gmag11/metatrader5_vnc container.
# Replaces the default /Metatrader/start.sh via volume mount in docker-compose.yml.

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
# MetaEditor path is resolved lazily at compile time (after MT5 installs) — see _resolve_metaeditor().
metaeditor=''
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

# ── [0/6] Restore experts.ini whitelist (127.0.0.1 socket access) ─
# MT5 stores the "Allow WebRequest for listed URL" entries in an encrypted
# binary Config/experts.ini keyed to the MachineGuid. Without 127.0.0.1 in
# the list, SocketConnect returns err=4014 and the EA never connects.
# Since MachineGuid is pinned (constant across restarts), the encrypted blob
# remains valid — so we can back it up once (after the operator adds 127.0.0.1
# via VNC) and restore it on every subsequent start.
_MT5_CONFIG_DIR_EARLY="/config/.wine/drive_c/Program Files/MetaTrader 5/Config"
EXPERTS_INI_LIVE="${_MT5_CONFIG_DIR_EARLY}/experts.ini"
EXPERTS_INI_BACKUP="/config/experts_ini.bak"
# Fresh-volume fallback: if a full Config/ backup exists but the live Config has
# no experts.ini, restore the backed-up files we don't already have (preserves
# the whitelist whichever file carries it on this build).
if [ -d /config/mt5_config_backup ] && [ ! -f "${EXPERTS_INI_LIVE}" ]; then
    mkdir -p "${_MT5_CONFIG_DIR_EARLY}"
    cp -rn /config/mt5_config_backup/. "${_MT5_CONFIG_DIR_EARLY}/" 2>/dev/null || true
    show_message "[0/6] Restored MT5 Config/ from full backup (whitelist preserved)."
fi
if [ -f "${EXPERTS_INI_BACKUP}" ] && [ ! -f "${EXPERTS_INI_LIVE}" ]; then
    mkdir -p "$(dirname "${EXPERTS_INI_LIVE}")"
    cp "${EXPERTS_INI_BACKUP}" "${EXPERTS_INI_LIVE}"
    show_message "[0/6] experts.ini restored from backup (127.0.0.1 whitelist preserved)."
elif [ -f "${EXPERTS_INI_BACKUP}" ] && [ -f "${EXPERTS_INI_LIVE}" ]; then
    # If the live file is smaller than the backup it was probably reset by MT5 — restore.
    _live_sz=$(stat -c%s "${EXPERTS_INI_LIVE}" 2>/dev/null || echo 0)
    _bak_sz=$(stat -c%s "${EXPERTS_INI_BACKUP}" 2>/dev/null || echo 0)
    if [ "${_live_sz}" -lt "${_bak_sz}" ]; then
        cp "${EXPERTS_INI_BACKUP}" "${EXPERTS_INI_LIVE}"
        show_message "[0/6] experts.ini restored from backup (live file was smaller/reset)."
    else
        show_message "[0/6] experts.ini live file OK."
    fi
else
    show_message "[0/6] No experts.ini backup yet — will save after ready_to_trade."
fi

# ── [0/6] Restore known-good chart profile (EA loads on the FIRST launch) ──
# MT5 decides which charts to OPEN on startup from the binary `order.wnd` file in
# the active profile dir — NOT from the mere presence of .chr files (MetaQuotes
# "Files and Folders" docs: order.wnd holds "the windows placement order"). Our
# inject_ea_chart.py writes chart01.chr but cannot synthesize order.wnd, so on a
# cold boot MT5 often opens ZERO charts and the EA never auto-loads (it only loads
# after the watchdog force-restart). The watchdog below snapshots a HEALTHY profile
# (chart01.chr + a matching order.wnd) once the EA is connected; restoring that
# snapshot here makes MT5 reopen chart01 WITH the EA on the very first launch —
# deterministic, no 2-minute kick, no manual drag.
_ACTIVE_PROFILE_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/MQL5/Profiles/Charts/Default"
_GOOD_PROFILE_SNAPSHOT="/config/good_profile"
if [ -f "${_GOOD_PROFILE_SNAPSHOT}/order.wnd" ]; then
    mkdir -p "${_ACTIVE_PROFILE_DIR}"
    cp -f "${_GOOD_PROFILE_SNAPSHOT}/"*.chr      "${_ACTIVE_PROFILE_DIR}/" 2>/dev/null || true
    cp -f "${_GOOD_PROFILE_SNAPSHOT}/order.wnd"  "${_ACTIVE_PROFILE_DIR}/" 2>/dev/null || true
    show_message "[0/6] Restored known-good chart profile (order.wnd + charts) — EA should load on first launch."
else
    show_message "[0/6] No known-good profile snapshot yet — watchdog will capture one once the EA connects."
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

# ── [3.5/6] ZMQ library (legacy — kept for volume compatibility) ──────────────
# EA v3 uses MT5 built-in sockets, not libzmq.dll. This section is a no-op
# on containers that already have /config/.zmq_installed marker. It only runs
# on fresh volumes — harmless but skipped immediately on restart.

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

# EA auto-load is handled by .chr injection (see the relaunch block after
# compile). The /config:[StartUp] mechanism was tried and does NOT work under
# this Wine/MT5 build — it left zero EA every time. A stale zonebot_startup.ini
# may linger in Config/ from that experiment; it's inert and harmless.

# ── Patch terminal.ini: enable AutoTrading ────────────────────────
# MT5 reads ExpertAdvisors=1 from [Common] in terminal.ini to allow EA
# execution. Without it the EA is loaded but silently disabled. MT5
# overwrites terminal.ini on exit, so we re-apply before every launch.
_patch_terminal_ini() {
    local _ini="${MT5_CONFIG_DIR}/terminal.ini"
    [ -f "${_ini}" ] || return 0
    python3 - "${_ini}" <<'_PATCH_PYEOF'
import sys
path = sys.argv[1]
data = open(path, 'rb').read()
if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
    text = data.decode('utf-16-le', errors='replace').lstrip('﻿')
    enc, bom = 'utf-16-le', b'\xff\xfe'
else:
    text = data.decode('utf-8', errors='replace')
    enc, bom = 'utf-8', b''
lines = text.splitlines()

# Track state for [Common] and [Expert] sections
in_common = False
in_expert  = False
has_experts_common = False   # ExpertAdvisors= in [Common]
has_dll_expert     = False   # AllowDll= in [Expert]
has_live_expert    = False   # AllowLive= in [Expert]
has_import_expert  = False   # AllowImport= in [Expert]
has_expert_section = False   # [Expert] section exists at all

new_lines = []
for line in lines:
    s = line.strip()
    # Section transitions
    if s == '[Common]':
        in_common = True
        in_expert  = False
    elif s == '[Expert]':
        in_common = False
        in_expert  = True
        has_expert_section = True
    elif s.startswith('[') and s.endswith(']'):
        # Leaving a section — flush any missing keys before the next section header
        if in_common and not has_experts_common:
            new_lines.append('ExpertAdvisors=1')
        if in_expert:
            if not has_dll_expert:    new_lines.append('AllowDll=1')
            if not has_live_expert:   new_lines.append('AllowLive=1')
            if not has_import_expert: new_lines.append('AllowImport=1')
        in_common = False
        in_expert  = False

    # Rewrite known keys to force-enable them
    if in_common and s.startswith('ExpertAdvisors='):
        new_lines.append('ExpertAdvisors=1'); has_experts_common = True; continue
    if in_expert and s.startswith('AllowDll='):
        new_lines.append('AllowDll=1');    has_dll_expert    = True; continue
    if in_expert and s.startswith('AllowLive='):
        new_lines.append('AllowLive=1');   has_live_expert   = True; continue
    if in_expert and s.startswith('AllowImport='):
        new_lines.append('AllowImport=1'); has_import_expert = True; continue

    new_lines.append(line)

# Flush if the last section was [Common] or [Expert] (no trailing section header)
if in_common and not has_experts_common:
    new_lines.append('ExpertAdvisors=1')
if in_expert:
    if not has_dll_expert:    new_lines.append('AllowDll=1')
    if not has_live_expert:   new_lines.append('AllowLive=1')
    if not has_import_expert: new_lines.append('AllowImport=1')

# If [Expert] section never existed at all, append it
if not has_expert_section:
    new_lines.append('')
    new_lines.append('[Expert]')
    new_lines.append('AllowDll=1')
    new_lines.append('AllowLive=1')
    new_lines.append('AllowImport=1')

result = '\r\n'.join(new_lines) + '\r\n'
open(path, 'wb').write(bom + result.encode(enc))
print('terminal.ini patched: ExpertAdvisors=1 AllowDll=1 AllowLive=1 AllowImport=1')
_PATCH_PYEOF
}
_patch_terminal_ini
show_message "terminal.ini patched for AutoTrading."

# ── Decide whether the EA needs recompiling — BEFORE the launch ───
# The old script deleted + recompiled the .ex5 on EVERY boot, which forced a
# compile-then-force-kill-then-relaunch cycle every single time. That restart
# disrupts MT5 right as the Python bot calls mt5.initialize(), producing the
# repeating "(-10005, IPC timeout) Terminal not running" loop: MT5 is mid
# broker-resync and not yet IPC-ready.
#
# New rule: only recompile when the EA source actually changed (or no .ex5
# exists). When the .ex5 is already current, inject the EA into the chart NOW —
# before the single MT5 launch — so MT5 loads it on startup with NO restart.
EA_SRC="/bot/src/infrastructure/mt5_bridge/ea/ZoneBotBridge.mq5"
EA_DST="${MT5_EXPERTS_DIR}/ZoneBotBridge.mq5"
EA_EX5="${MT5_EXPERTS_DIR}/ZoneBotBridge.ex5"
_recompile_needed=0
if [ ! -f "${EA_EX5}" ]; then
    _recompile_needed=1
    show_message "EA: no compiled .ex5 yet — will compile, then restart MT5 once."
elif [ "${EA_SRC}" -nt "${EA_EX5}" ]; then
    _recompile_needed=1
    show_message "EA: source newer than .ex5 — will recompile, then restart MT5 once."
else
    show_message "EA: .ex5 up to date — injecting into chart before launch (no restart)."
    SYMBOLS_CSV="${SYMBOLS_CSV:-}" python3 /bot/tools/inject_ea_chart.py || true
fi

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

show_message "[5.5/6] pyzmq skipped — EA v3 uses MT5 built-in sockets; Python side uses plain TCP."

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

# Back up the whitelist now that the operator has confirmed setup.
# The "Allow WebRequest for listed URL" entries (which authorize SocketConnect
# to 127.0.0.1, fixing err=4014) are stored encrypted in MT5's Config dir,
# keyed to the MachineGuid. MachineGuid is pinned, so the blob stays valid.
# The exact filename varies by build (experts.ini on some, folded into the
# encrypted terminal config on others) — so back up every .ini in Config/ and
# log what's actually there so we capture whichever file carries the whitelist.
if [ -f "${EXPERTS_INI_LIVE}" ]; then
    cp "${EXPERTS_INI_LIVE}" "${EXPERTS_INI_BACKUP}"
    show_message "experts.ini backed up to ${EXPERTS_INI_BACKUP} (whitelist will auto-restore on future starts)."
else
    show_message "NOTE: ${EXPERTS_INI_LIVE} not present on this build. Config/ contents:"
    ls -la "${MT5_CONFIG_DIR}" 2>/dev/null | sed 's/^/    /' || true
    # Back up the whole Config dir as a fallback so no whitelist file is missed.
    if [ -d "${MT5_CONFIG_DIR}" ]; then
        rm -rf /config/mt5_config_backup
        cp -r "${MT5_CONFIG_DIR}" /config/mt5_config_backup 2>/dev/null \
            && show_message "Full Config/ backed up to /config/mt5_config_backup." \
            || show_message "WARNING: Config/ backup failed."
    fi
fi

# ── Deploy and compile ZoneBotBridge EA (only when needed) ────────
# Ensure directories exist (MT5 should have created them, but be safe)
mkdir -p "${MT5_EXPERTS_DIR}" "${MT5_CONFIG_DIR}"

# EA v3 uses MT5 built-in sockets — no ZMQ DLLs needed for compile or runtime.

# When the .ex5 is already current we pre-injected the EA before launch and MT5
# is loading it now — skip the whole compile + restart cycle entirely.
if [ "${_recompile_needed}" -eq 0 ]; then
    show_message "EA already compiled and pre-injected — skipping recompile/restart."
elif [ -f "${EA_SRC}" ]; then
    cp "${EA_SRC}" "${EA_DST}"
    show_message "ZoneBotBridge.mq5 deployed to MQL5/Experts/"
    # Remove any stale .ex5 so the "compiled successfully" check below is real.
    rm -f "${EA_EX5}"

    # Compile with MetaEditor if available.
    # MetaEditor needs MT5 to be fully authorized and MQL5 environment loaded
    # before it can compile — if we run it too early the compile silently
    # produces nothing.  Poll the MT5 log for "terminal synchronized" (which
    # only appears AFTER the broker handshake is complete) before compiling.
    #
    # Resolve metaeditor path HERE (after MT5 install) — not at script start
    # where MT5 may not be installed yet (fresh volume → binary missing).
    if [ -e '/config/.wine/drive_c/Program Files/MetaTrader 5/MetaEditor64.exe' ]; then
        metaeditor='/config/.wine/drive_c/Program Files/MetaTrader 5/MetaEditor64.exe'
    elif [ -e '/config/.wine/drive_c/Program Files/MetaTrader 5/metaeditor64.exe' ]; then
        metaeditor='/config/.wine/drive_c/Program Files/MetaTrader 5/metaeditor64.exe'
    fi
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

# ── Relaunch MT5 with the EA injected into its active chart profile ──
# MT5 was first launched (above) before the .ex5 existed, so it has no EA.
# Now that ZoneBotBridge.ex5 is compiled, stop MT5, inject the EA into the
# .chr file of the profile MT5 actually loads, and relaunch normally.
#
# WHY injection and NOT [StartUp]: the /config:[StartUp] mechanism does not
# work under this Wine build (MT5 ignores the directive — "NOT loaded within
# 75s" every time). The .chr-injection approach IS proven to work here: MT5
# reopens the charts present in its active profile directory on launch, and a
# chart carrying the <expert> block loads the EA. We do NOT empty the profile
# (that removed the very charts MT5 reopens, which is why [StartUp] left us
# with zero EA). inject_ea_chart.py enforces exactly ONE instance.
#
# This restart ONLY runs when we just (re)compiled — i.e. the EA wasn't loaded
# at the initial launch because the .ex5 didn't exist yet. On a normal restart
# the .ex5 was current, we pre-injected before launch, and MT5 already has the
# EA — so we skip this disruptive cycle and avoid the IPC-timeout window.
if [ "${_recompile_needed}" -eq 1 ] && [ -f "${EA_EX5}" ]; then
    # Count existing EA loads so the verify poll below only accepts a NEW one
    # (the persistent journal carries stale "loaded successfully" lines).
    _ea_loads_before=$(python3 -c "
import glob,os
ls=sorted(glob.glob('/config/.wine/drive_c/Program Files/MetaTrader 5/logs/*.log'),key=os.path.getmtime)
n=0
for l in ls:
    n+=open(l,'rb').read().decode('utf-16-le','replace').count('ZoneBotBridge')
print(n)
" 2>/dev/null || echo 0)

    show_message "Stopping MT5 to inject EA into its active chart profile..."
    pgrep -f "terminal64.exe" > /dev/null 2>&1 && \
        $wine_executable taskkill /IM terminal64.exe /F 2>/dev/null || true
    sleep 5
    # Inject the EA into the profile MT5 reloads (single instance enforced).
    SYMBOLS_CSV="${SYMBOLS_CSV:-}" python3 /bot/tools/inject_ea_chart.py || true
    _patch_terminal_ini
    # Launch MT5 normally — it reopens its profile charts (now carrying the EA).
    DISPLAY=:1 WINEPREFIX=/config/.wine WINEDEBUG=-all \
        $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
    show_message "MT5 relaunched with injected EA — verifying EA load in background (bot starts now)."
    # Verify a NEW EA load appeared (count increased) — not a stale journal line.
    # Run in BACKGROUND so the bot starts immediately; after a force-kill MT5 must
    # reconnect to the broker and re-synchronize before it opens charts and loads
    # the EA, which routinely takes 1-2 min — longer than any reasonable inline
    # wait. The bot's TcpFeed listens independently, so blocking here is pointless.
    (
        _ea_loads_before="${_ea_loads_before}" python3 - <<'VERIFY_PYEOF'
import glob, os, time
logdir = "/config/.wine/drive_c/Program Files/MetaTrader 5/logs"
before = int(os.environ.get("_ea_loads_before", "0"))
deadline, hit = time.time() + 180, None
while time.time() < deadline and not hit:
    loads = []
    for l in sorted(glob.glob(os.path.join(logdir, "*.log")), key=os.path.getmtime):
        data = open(l, "rb").read().decode("utf-16-le", errors="replace")
        loads += [ln.strip() for ln in data.splitlines()
                  if "ZoneBotBridge" in ln and "loaded successfully" in ln]
    if len(loads) > before:
        hit = loads[-1]
    else:
        time.sleep(5)
print("[ea-verify]", "EA loaded:", hit) if hit else print(
    "[ea-verify] EA not seen in journal after 180s. The EA still loads once MT5 "
    "finishes broker sync — confirm via 'TcpFeed: EA connected' in the bot log.")
VERIFY_PYEOF
    ) &
fi

# ── MT5 auto-heal watchdog (power-loss / crash safety net) ────────
# The graceful-shutdown trap below handles PLANNED stops (it lets MT5 save its
# chart+EA session so the next boot restores it). But a power cut / OOM kill /
# docker kill sends no signal — MT5 never saves, so on the next boot the profile
# may have no charts and the EA never auto-loads (MetaQuotes docs: "if the
# current profile has no charts, the Expert Advisor will not be started").
#
# This watchdog makes the EA connection self-healing regardless of HOW MT5 last
# died. It watches the ONE thing we actually care about: is the EA's TCP client
# connected to our feed on port 5556? We read that straight from the kernel via
# /proc/net/tcp (no `ss`/`netstat` dependency, no log parsing):
#   ESTAB  → an EA is connected      → healthy, do nothing
#   LISTEN → bot listening, no EA yet → start counting; if it persists, kick MT5
#   NONE   → bot not listening yet    → still starting up, never kick
# Force-kill + re-inject + relaunch repeats until the EA connects. It only ever
# acts at startup or after a failure — once connected it sits idle, so there is
# ZERO per-tick latency on the live feed.
#
# WHY /proc/net/tcp and pgrep (not `wine tasklist`): wine tasklist opens its own
# wineserver context and sees no processes; the kernel process/socket tables are
# always authoritative regardless of wineserver state.
_FEED_PORT=5556
_feed_state() {
    _FEED_PORT="${_FEED_PORT}" python3 - <<'PYEOF'
import os
port = int(os.environ.get("_FEED_PORT", "5556"))
hexport = format(port, "04X")
listen = estab = False
for f in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        for line in open(f).read().splitlines()[1:]:
            p = line.split()
            if len(p) < 4:
                continue
            if p[1].rsplit(":", 1)[-1] != hexport:
                continue
            if p[3] == "0A":      # TCP_LISTEN
                listen = True
            elif p[3] == "01":    # TCP_ESTABLISHED
                estab = True
    except Exception:
        pass
print("ESTAB" if estab else ("LISTEN" if listen else "NONE"))
PYEOF
}

_relaunch_mt5() {
    # MT5 is being (re)started — re-inject the EA (idempotent) and patch
    # AutoTrading flags so the profile MT5 reopens carries the EA.
    SYMBOLS_CSV="${SYMBOLS_CSV:-}" python3 /bot/tools/inject_ea_chart.py || true
    _patch_terminal_ini
    DISPLAY=:1 WINEPREFIX=/config/.wine WINEDEBUG=-all \
        $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
}

# Snapshot the CURRENTLY-OPEN, healthy profile (its .chr files AND the order.wnd
# that lists chart01 as open) so the next boot can restore it and MT5 opens
# chart01 + the EA on the first launch. Called only when the EA is connected, so
# order.wnd is guaranteed to reference the chart the EA lives on.
_snapshot_good_profile() {
    [ -d "${_ACTIVE_PROFILE_DIR}" ] || return 0
    [ -f "${_ACTIVE_PROFILE_DIR}/order.wnd" ] || return 0   # nothing to capture yet
    rm -rf "${_GOOD_PROFILE_SNAPSHOT}.tmp"
    mkdir -p "${_GOOD_PROFILE_SNAPSHOT}.tmp"
    cp -f "${_ACTIVE_PROFILE_DIR}/"*.chr     "${_GOOD_PROFILE_SNAPSHOT}.tmp/" 2>/dev/null || true
    cp -f "${_ACTIVE_PROFILE_DIR}/order.wnd" "${_GOOD_PROFILE_SNAPSHOT}.tmp/" 2>/dev/null || true
    # Commit atomically only if we captured the order.wnd (the key file).
    if [ -f "${_GOOD_PROFILE_SNAPSHOT}.tmp/order.wnd" ]; then
        rm -rf "${_GOOD_PROFILE_SNAPSHOT}"
        mv "${_GOOD_PROFILE_SNAPSHOT}.tmp" "${_GOOD_PROFILE_SNAPSHOT}"
        show_message "[watchdog] Snapshotted healthy chart profile (order.wnd + charts) — first-launch EA load is now persistent."
    else
        rm -rf "${_GOOD_PROFILE_SNAPSHOT}.tmp"
    fi
}

_WATCHDOG_INTERVAL=20
_HEAL_AFTER_SECS=90          # bot listening but no EA for this long → kick MT5
_MAX_HEALS=3                 # after this many failed kicks, STOP thrashing MT5
(
    _no_feed=0
    _snapshotted=0
    _heals=0
    _gaveup=0
    while true; do
        sleep ${_WATCHDOG_INTERVAL}

        # 1) MT5 process gone entirely → relaunch immediately.
        if ! pgrep -f "terminal64.exe" > /dev/null 2>&1; then
            show_message "[watchdog] MT5 not running — re-injecting EA and relaunching..."
            _relaunch_mt5
            _no_feed=0
            sleep 30
            continue
        fi

        # 2) MT5 is up — is the EA actually connected to our feed?
        case "$(_feed_state)" in
            ESTAB)
                _no_feed=0            # healthy — EA streaming ticks
                _heals=0             # connection is good → re-arm healing
                _gaveup=0
                # Capture the working profile ONCE per session (after a short
                # settle so MT5 has flushed order.wnd with chart01 open). Next
                # boot restores it → EA loads on the first launch. This is also
                # how a one-time manual drag gets persisted automatically.
                if [ ${_snapshotted} -eq 0 ]; then
                    sleep 10
                    _snapshot_good_profile
                    _snapshotted=1
                fi
                ;;
            LISTEN)
                # Bot is listening but the EA hasn't connected.
                if [ ${_gaveup} -eq 1 ]; then
                    # Already exhausted auto-load attempts — do NOT thrash MT5.
                    # Wait quietly for a one-time manual attach (which we'll snapshot).
                    continue
                fi
                _no_feed=$((_no_feed + _WATCHDOG_INTERVAL))
                if [ ${_no_feed} -ge ${_HEAL_AFTER_SECS} ]; then
                    if [ ${_heals} -lt ${_MAX_HEALS} ]; then
                        _heals=$((_heals + 1))
                        show_message "[watchdog] EA not connected to :${_FEED_PORT} for ${_no_feed}s — force-restarting MT5 (attempt ${_heals}/${_MAX_HEALS})..."
                        pgrep -f "terminal64.exe" > /dev/null 2>&1 && \
                            $wine_executable taskkill /IM terminal64.exe /F 2>/dev/null || true
                        sleep 5
                        _relaunch_mt5
                        _no_feed=0
                        sleep 45      # let MT5 reconnect + load EA before re-checking
                    else
                        _gaveup=1
                        show_message "════════════════════════════════════════════════════════════"
                        show_message "[watchdog] MT5 did NOT auto-load the EA after ${_MAX_HEALS} restarts."
                        show_message "  This MT5/Wine build won't restore the EA on its own. ONE-TIME fix:"
                        show_message "    1. Open VNC: http://localhost:3001"
                        show_message "    2. Drag 'ZoneBotBridge' from Navigator onto the GBPUSD,H1 chart"
                        show_message "       (Allow Algo Trading when prompted)."
                        show_message "  It will connect, and this script auto-snapshots the working profile"
                        show_message "  (chart + order.wnd) so it loads by itself on every future start."
                        show_message "  No more thrashing — waiting quietly for the EA to connect."
                        show_message "════════════════════════════════════════════════════════════"
                    fi
                fi
                ;;
            *)
                # NONE — bot's TcpFeed not listening yet (still starting). Never
                # kick MT5 during this window; just wait for the feed to come up.
                _no_feed=0
                ;;
        esac
    done
) &
_watchdog_pid=$!
show_message "MT5 auto-heal watchdog started (monitors EA connection on :${_FEED_PORT})."

# ── Wait until MT5 IPC is actually reachable before starting the bot ─
# The bot's mt5.initialize() attaches to the RUNNING terminal (MT5_PATH is
# empty, so it does not launch one). MT5 only answers that IPC pipe AFTER it
# finishes its Wine cold-start + broker handshake; until then initialize()
# returns (-10005, IPC timeout).
#
# Rather than guess MT5's readiness from journal strings (fragile — the daily
# log carries stale lines and the exact wording varies by build), we PROBE the
# real capability: run a throwaway wine-python that just calls
# mt5.initialize() and shuts down. The moment that succeeds, the bot's own
# initialize() will succeed too. This is the exact thing that was failing, so
# it is the correct gate — and it can never hang on a missing log string.
_probe_timeout=300
_probe_wait=0
show_message "Probing MT5 IPC readiness before starting bot (up to ${_probe_timeout}s)..."
while [ ${_probe_wait} -lt ${_probe_timeout} ]; do
    # The probe prints the exact reason on each failure (import fault vs IPC
    # not ready vs which path works) so we can diagnose instead of guess.
    _probe_out=$(DISPLAY=:1 WINEPREFIX=/config/.wine PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
        $wine_executable python /bot/tools/mt5_probe.py 2>/dev/null)
    _probe_rc=$?
    show_message "  [probe ${_probe_wait}s] ${_probe_out}"
    if [ ${_probe_rc} -eq 0 ]; then
        show_message "MT5 IPC reachable after ${_probe_wait}s — starting bot."
        break
    fi
    sleep 10
    _probe_wait=$((_probe_wait + 10))
done
if [ ${_probe_wait} -ge ${_probe_timeout} ]; then
    show_message "MT5 IPC probe timed out after ${_probe_timeout}s — starting bot anyway."
    show_message "  If you see repeated 'IPC timeout', MT5 likely isn't logging in:"
    show_message "  open VNC (http://localhost:3001) and confirm the terminal is connected."
fi

# ── Graceful shutdown so MT5 PERSISTS its chart + attached EA ─────
# THE persistence fix. `docker compose down` sends SIGTERM and the container is
# killed seconds later — if MT5 is hard-killed it does NOT save its session, so
# on the next boot it may open with no chart and the EA never auto-loads (even
# though chart01.chr holds it on disk). That is exactly the intermittent
# "have to drag the EA again" symptom.
#
# Here we trap SIGTERM/SIGINT and close MT5 *cleanly* (taskkill WITHOUT /F = a
# normal window close). A clean close makes MT5 persist its open chart and the
# expert attached to it, so on the next start MT5 restores the EA itself — no
# manual drag, deterministically. We stop the watchdog first so it can't
# relaunch MT5 mid-shutdown.
_graceful_shutdown() {
    show_message "Container stopping — closing MT5 cleanly so it saves the chart+EA session..."
    # Stop the watchdog so it doesn't relaunch MT5 while we're closing it.
    [ -n "${_watchdog_pid:-}" ] && kill "${_watchdog_pid}" 2>/dev/null || true
    # Clean close (NO /F): lets MT5 write its profile/lastsession with the EA.
    pgrep -f "terminal64.exe" > /dev/null 2>&1 && \
        $wine_executable taskkill /IM terminal64.exe 2>/dev/null || true
    # Give MT5 time to flush the session to disk before the container dies.
    sleep 8
    # Tell the bot to stop (its own SIGTERM handler flushes a checkpoint).
    [ -n "${_bot_pid:-}" ] && kill "${_bot_pid}" 2>/dev/null || true
    wait "${_bot_pid:-}" 2>/dev/null || true
    show_message "Clean shutdown complete — EA session saved for next start."
    exit 0
}
trap _graceful_shutdown SIGTERM SIGINT

# ── START BOT ─────────────────────────────────────────────────────
# Launch the event-driven streaming bot (main_stream.py).
# Wine reports os.name == "nt" so mt5_gateway.py uses direct
# MetaTrader5 import — no RPyC bridge.
# PYTHONUTF8=1 forces UTF-8 to prevent cp1252 crashes on box-drawing chars.
# Run in BACKGROUND + wait so the trap above can fire on container stop
# (a foreground command would swallow the signal until it returns).
echo "Starting ZoneBot (stream mode) in Wine Python..."
cd /bot && DISPLAY=:1 WINEPREFIX=/config/.wine PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
    $wine_executable python -m src.main_stream &
_bot_pid=$!
# `wait` returns when the bot exits OR when a trapped signal fires.
wait "${_bot_pid}"
