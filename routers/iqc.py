from templates_config import templates
import csv
import io
from datetime import datetime as _dtnow
from decimal import Decimal, InvalidOperation
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS, DROPDOWN_STAGES
from models.lot import Lot, LotLineItem
from models.iqc_inspection import IQCInspection
from models.location import StorageLocation, DeviceLocationLog, LocationAction
from models.work_order import WorkOrder
from auth.dependencies import (get_current_user, require_roles, verify_csrf, require_module_perm,
                               require_any_module_perm, require_additional_perm, require_any_additional_perm)
from services.audit_engine import audit
from services.control_engine import validate_transition
from services.location_defaults import ensure_stage_location
from utils.master_data import master_values
from utils.grades import parse_grade
from routers.devices import _build_model_summary

router = APIRouter(prefix="/iqc", tags=["iqc"], dependencies=[Depends(verify_csrf)])


def _keep_after_rollback(db, obj):
    """Detach `obj` so it stays readable once the session is rolled back.

    session.rollback() expires every instance the session manages. The next
    attribute read then triggers a lazy refresh, and under async SQLAlchemy that
    implicit IO raises MissingGreenlet. Since these error paths roll back and
    *then* render a template that reads current_user, the friendly form error
    turned into a 500 — the failure users were actually hitting. Detaching first
    keeps the already-loaded values usable without further IO.

    MUST be called BEFORE `await db.rollback()`, never after (every call site
    follows this). It was briefly "fixed" the other way round 2026-09-02
    while chasing an /iqc/new 500, on the theory that a failure mid-flush
    leaves the session in a "pending rollback" state where expunge() itself
    raises — true, but swapping the order made things worse: rollback()
    first expires obj's attributes, and expunge() second then detaches it
    *with those attributes already expired*, which is precisely what
    DetachedInstanceError requires — reproducing the exact crash this
    function exists to prevent, just one line later. The correct fix for
    the mid-flush case is what's already here: expunge()'s failure is
    caught and swallowed below, `obj` stays merely attached (not detached)
    rather than expunged, and the *next* line's rollback() still runs
    regardless — clearing the pending-rollback state so obj can lazy-refresh
    normally off the now-healthy session instead of crashing. The actual
    /iqc/new bug was unrelated to this ordering: an unguarded
    `current_user.username` read inside _iqc_form_boundary's own diagnostic
    print(), before rollback ever ran — see that function.
    """
    try:
        db.expunge(obj)
    except Exception:
        pass


def _iqc_form_boundary(fn):
    """Never let New IQC Entry fail with a bare 500.

    The handler guards the failures it can predict (bad UUID, bad enum,
    constraint violations at flush/commit), but anything it did not predict
    reached the user as an opaque "500" with the whole form wiped — and, since
    the technician cannot report what they cannot see, with nothing to diagnose
    from either.

    Any unhandled exception now rolls back, logs a full traceback server-side,
    and re-renders the form with the actual error text on screen, so the person
    hitting it can read and report it. HTTPException is left alone: 403s and
    redirects from the auth layer are deliberate control flow, not faults.
    """
    import functools
    import traceback as _tb

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            db = kwargs.get("db")
            request = kwargs.get("request")
            current_user = kwargs.get("current_user")
            # This is THE bug behind the /iqc/new 500s users actually hit
            # (found 2026-09-02): a failure mid-flush leaves the session in
            # SQLAlchemy's "pending rollback" state, where reading ANY
            # not-yet-loaded attribute on current_user — getattr's default
            # only covers a missing attribute, not one that raises while
            # being read — tries to lazy-refresh against the poisoned
            # session and raises right here, before rollback() below ever
            # runs. The exception this print() was meant to log instead
            # replaced it with a second, opaque one, and the print() call
            # itself never completed — which is also why "IQC ENTRY FAILED"
            # never once reached the server log despite this happening.
            try:
                username_for_log = current_user.username if current_user is not None else "?"
            except Exception:
                username_for_log = "?"
            print(f"\n{'='*60}\nIQC ENTRY FAILED for "
                  f"{username_for_log} "
                  f"barcode={kwargs.get('barcode', '?')!r}\n"
                  f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}{'='*60}",
                  flush=True)
            lots = []
            if db is not None:
                try:
                    # Expunge BEFORE rollback (original, correct order —
                    # briefly "fixed" the other way 2026-09-02 and made it
                    # worse, see _keep_after_rollback's docstring): rollback()
                    # expires every attribute the session is tracking, so
                    # detaching current_user first preserves whatever's
                    # already loaded (used to render the template below) as
                    # plain in-memory values. If the session is already in a
                    # "pending rollback" state (a failure mid-flush) expunge()
                    # can itself raise here — harmlessly: it's caught, and
                    # rollback() still runs on the next line regardless,
                    # clearing that state so current_user (now merely still
                    # attached rather than detached) can lazy-refresh
                    # normally off the healthy session instead of crashing.
                    if current_user is not None:
                        _keep_after_rollback(db, current_user)
                    await db.rollback()
                    lots = (await db.execute(
                        select(Lot).order_by(Lot.lot_number))).scalars().all()
                except Exception:
                    lots = []
            try:
                return templates.TemplateResponse("iqc/form.html", {
                    "request": request, "lots": lots, "current_user": current_user,
                    "prefill_lot_id": kwargs.get("lot_id", ""),
                    "prefill_grn": kwargs.get("grn_number", ""),
                    "error": f"Could not save this IQC entry — {type(exc).__name__}: "
                             f"{str(exc)[:300]}. Please screenshot this message.",
                }, status_code=200)
            except Exception:
                # Last resort: a sufficiently severe session failure (a hard
                # constraint violation mid-flush, in the rarest cases) can
                # leave current_user unusable for base.html's own reads
                # (role/username in the sidebar) even after the expunge/
                # rollback above — Jinja renders synchronously and eagerly
                # (Starlette's TemplateResponse.__init__ calls template.render()
                # immediately), so that failure surfaces right here, not
                # later. Redirecting (a fresh request, fresh session, fresh
                # current_user) can never hit this same failure mode — it is
                # the one response that is guaranteed not to need the
                # request-scoped session or current_user object at all.
                return RedirectResponse(
                    url="/iqc/new?error=Could+not+save+this+entry+—+please+retry+and+screenshot+if+it+happens+again",
                    status_code=302)

    return wrapper
allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.iqc_inspector,
                         UserRole.sales_manager)


def _find_usb_iqc_file():
    """Scan removable drives for the OxyQC 'latest' inspection file written by the
    USB app (<USB>\\oxyqc_offline\\latest_iqc.json). Returns Path or None."""
    import platform
    from pathlib import Path
    candidates_rel = ["oxyqc_offline/latest_iqc.json", "latest_iqc.json"]
    roots = []
    if platform.system() == "Windows":
        try:
            import ctypes, string
            DRIVE_REMOVABLE = 2
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    root = f"{letter}:\\"
                    if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOVABLE:
                        roots.append(Path(root))
                bitmask >>= 1
        except Exception:
            pass
    else:
        for base in ("/media", "/mnt", "/run/media"):
            p = Path(base)
            if p.exists():
                roots.extend(p.glob("*"))
    best = None
    for r in roots:
        for rel in candidates_rel:
            f = r / rel
            try:
                if f.exists() and (best is None or f.stat().st_mtime > best.stat().st_mtime):
                    best = f
            except Exception:
                pass
    return best


# ── "Diagnose this Device": read the HOST machine's hardware via WMI/CIM and
#    map it to the IQC form fields. NOTE: this detects the machine running the
#    web app (server / local inspection station), not a remote browser client.
_PS_DIAGNOSE = r'''
$ErrorActionPreference='SilentlyContinue'
$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1
$cs=Get-CimInstance Win32_ComputerSystem
$bios=Get-CimInstance Win32_BIOS|Select-Object -First 1
$os=Get-CimInstance Win32_OperatingSystem
$encl=Get-CimInstance Win32_SystemEnclosure|Select-Object -First 1
$batt=Get-CimInstance Win32_Battery|Select-Object -First 1
$gpu=Get-CimInstance Win32_VideoController|Where-Object {$_.Name -notmatch 'Basic|Remote|Meta|Mirror|DisplayLink|USB'}|Select-Object -First 1
$pd=@(Get-PhysicalDisk|Where-Object {$_.BusType -ne 'USB' -and $_.Size -gt 8GB}|ForEach-Object{[ordered]@{type="$($_.MediaType)";sizeGB=[math]::Round($_.Size/1GB);make="$($_.FriendlyName)".Trim();rpm=$_.SpindleSpeed}})
$ram=@(Get-CimInstance Win32_PhysicalMemory -EA SilentlyContinue|ForEach-Object{[ordered]@{capacityGB=[math]::Round($_.Capacity/1GB);speed=$_.Speed;make="$($_.Manufacturer)".Trim();memType=$_.SMBIOSMemoryType}})
$bh=$null
$full=(Get-CimInstance -Namespace root\wmi -ClassName BatteryFullChargedCapacity -EA SilentlyContinue|Select-Object -First 1).FullChargedCapacity
$des=(Get-CimInstance -Namespace root\wmi -ClassName BatteryStaticData -EA SilentlyContinue|Select-Object -First 1).DesignedCapacity
if($des -and $des -gt 0 -and $full -and $full -gt 0){$bh=[math]::Round(($full/$des)*100)}
if(-not $bh -and $batt){$fc=$batt.FullChargeCapacity;$dc=$batt.DesignCapacity;if($fc -gt 0 -and $dc -gt 0){$bh=[math]::Round($fc/$dc*100)}}
if(-not $bh -and $batt){try{$rf=[System.IO.Path]::Combine($env:TEMP,'bh_'+[guid]::NewGuid().ToString('N').Substring(0,8)+'.html');$null=powercfg /batteryreport /output $rf 2>&1;if(Test-Path $rf){$h=Get-Content $rf -Raw;Remove-Item $rf -Force -EA SilentlyContinue;$m1=$null;$m2=$null;if($h -match '(?si)DESIGN CAPACITY.{0,200}?([\d,]+)\s*mWh'){$m1=[int64]($matches[1]-replace',','')};if($h -match '(?si)FULL CHARGE CAPACITY.{0,200}?([\d,]+)\s*mWh'){$m2=[int64]($matches[1]-replace',','')};if($m1 -gt 0 -and $m2 -gt 0){$bh=[math]::Round($m2/$m1*100)}}}catch{}}
$scr=$null
$mons=@(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorBasicDisplayParams -EA SilentlyContinue)
foreach($mn in $mons){if($mn.MaxHorizontalImageSize -gt 0 -and $mn.MaxVerticalImageSize -gt 0){$dd=[math]::Round([math]::Sqrt(($mn.MaxHorizontalImageSize*$mn.MaxHorizontalImageSize)+($mn.MaxVerticalImageSize*$mn.MaxVerticalImageSize))/2.54,1);if($dd -gt 5 -and $dd -lt 40){if(-not $scr -or $dd -lt $scr){$scr=$dd}}}}
[ordered]@{
 manufacturer="$($cs.Manufacturer)";model="$($cs.Model)";serial="$($bios.SerialNumber)";
 cpu="$(($cpu.Name).Trim())";cpu_make="$($cpu.Manufacturer)".Trim();cores=$cpu.NumberOfCores;ram_gb=[math]::Round($cs.TotalPhysicalMemory/1GB);
 chassis=@($encl.ChassisTypes);has_battery=[bool]$batt;battery_pct=$batt.EstimatedChargeRemaining;battery_health=$bh;
 screen_in=$scr;gpu="$($gpu.Name)";os="$($os.Caption)";disks=$pd;ram_sticks=$ram
} | ConvertTo-Json -Depth 5 -Compress
'''

_SMBIOS_RAM_TYPE = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5",
                    28: "LPDDR", 29: "LPDDR2", 30: "LPDDR3", 31: "LPDDR4"}


def _ram_type_label(mem_type):
    if mem_type is None or mem_type == "":
        return ""
    if isinstance(mem_type, str) and not mem_type.isdigit():
        return mem_type.upper().replace(" ", "")
    try:
        return _SMBIOS_RAM_TYPE.get(int(mem_type), "")
    except (TypeError, ValueError):
        return ""


def _format_ram_summary(sticks):
    """See oxyqc-agent/oxyqc_agent.py's format_ram_summary — same packed-string
    format, duplicated here since this is a separate server-side probe path."""
    parts = []
    for s in sticks or []:
        gb = s.get("capacityGB")
        if not gb:
            continue
        segs = [f"{int(gb)}GB"]
        t = _ram_type_label(s.get("memType"))
        if t:
            segs.append(t)
        speed = str(s.get("speed") or "").strip()
        if speed and speed != "0":
            segs.append(speed)
        make = str(s.get("make") or "").strip()
        if make:
            segs.append(make)
        parts.append("_".join(segs))
    return ", ".join(parts)


def _format_hdd_summary(drives):
    """See oxyqc-agent/oxyqc_agent.py's format_hdd_summary — same packed-string
    format, duplicated here since this is a separate server-side probe path."""
    parts = []
    for d in drives or []:
        gb = d.get("sizeGB")
        if not gb:
            continue
        gb = int(gb)
        size_label = f"{round(gb / 1000)}TB" if gb >= 1000 else f"{gb}GB"
        segs = [size_label]
        dtype = str(d.get("type") or "").strip().upper()
        if dtype:
            segs.append(dtype)
        rpm = d.get("rpm")
        if rpm:
            segs.append(str(int(rpm)))
        make = str(d.get("make") or "").strip()
        if make:
            segs.append(make)
        parts.append("_".join(segs))
    return ", ".join(parts)

_STD_CAPACITIES = [32, 64, 120, 128, 240, 256, 320, 480, 500, 512, 640, 750, 1000, 1024, 2000, 2048, 4000, 4096]


def _snap_capacity(gb):
    try:
        gb = int(gb)
    except (TypeError, ValueError):
        return None
    if gb <= 0:
        return None
    return min(_STD_CAPACITIES, key=lambda s: abs(s - gb))


def _intel_gen(cpu):
    import re
    if not cpu:
        return None
    if "Core Ultra" in cpu:
        return "Core Ultra"
    m = re.search(r"i[3579][- ]?(\d{3,5})", cpu)
    if m:
        n = m.group(1)
        g = n[:2] if len(n) >= 5 else (n[:1] if len(n) == 4 else None)
        if g:
            return f"{int(g)}th Gen"
    return None


def _generation(cpu):
    """Generation across Intel / AMD Ryzen / Apple M-series. Extends _intel_gen."""
    import re
    g = _intel_gen(cpu)
    if g:
        return g
    if cpu:
        m = re.search(r"Ryzen\s+([3579])", cpu)
        if m:
            return f"AMD Ryzen {m.group(1)}"
        m2 = re.search(r"Apple\s+(M\d+)\s*(Pro|Max|Ultra)?", cpu)
        if m2:
            return f"Apple {m2.group(1)}{(' ' + m2.group(2)) if m2.group(2) else ''}"
    return None


def _cpu_make(cpu, manufacturer=None):
    """Derive Intel / AMD / Apple from the CPU manufacturer string or, failing
    that, by parsing the CPU name."""
    import re
    src = f"{manufacturer or ''} {cpu or ''}".lower()
    if "intel" in src or "genuineintel" in src:
        return "Intel"
    if "amd" in src or "authenticamd" in src or "ryzen" in src:
        return "AMD"
    if "apple" in src or re.match(r"^\s*m\d", (cpu or "").strip(), re.I):
        return "Apple"
    return None


def _detect_host_hardware():
    """Run the WMI/CIM probe and map it to IQC form-field keys. Returns (fields, error)."""
    import subprocess, json as _json, shutil, platform
    ps = shutil.which("powershell") or "powershell"
    kw = {}
    if platform.system() == "Windows":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", _PS_DIAGNOSE],
                           capture_output=True, text=True, timeout=45, **kw)
    except Exception as e:
        return None, f"hardware probe failed: {e}"
    raw = (r.stdout or "").strip()
    if not raw:
        return None, ((r.stderr or "no output from hardware probe").strip()[:200])
    try:
        info = _json.loads(raw)
    except Exception:
        return None, "could not parse hardware probe output"

    chassis = info.get("chassis") or []
    if isinstance(chassis, int):
        chassis = [chassis]
    laptop_codes = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
    is_laptop = bool(info.get("has_battery")) or any(
        str(c).isdigit() and int(c) in laptop_codes for c in chassis)
    sub = "Laptop" if is_laptop else "Desktop"

    disks = info.get("disks") or []
    if isinstance(disks, dict):
        disks = [disks]
    ssd = [d for d in disks if str(d.get("type", "")).upper().startswith("SSD")]
    hdd = [d for d in disks if str(d.get("type", "")).upper().startswith("HDD")]

    f = {}
    if info.get("manufacturer"):
        f["brand"] = str(info["manufacturer"]).split()[0].title()
    if info.get("model"):
        f["model"] = info["model"]
    serial = str(info.get("serial") or "").strip()
    if serial and serial not in ("To Be Filled By O.E.M.", "Default string", "System Serial Number", "None"):
        f["serial_no"] = serial
    if info.get("cpu"):
        f["cpu"] = info["cpu"]
    make = _cpu_make(info.get("cpu"), info.get("cpu_make"))
    if make:
        f["cpu_make"] = make
    gen = _generation(info.get("cpu"))
    if gen:
        f["generation"] = gen
    if info.get("ram_gb"):
        try:
            f["ram_gb"] = int(info["ram_gb"])
        except (TypeError, ValueError):
            pass
    ram_sticks = info.get("ram_sticks") or []
    if isinstance(ram_sticks, dict):
        ram_sticks = [ram_sticks]
    ram_summary = _format_ram_summary(ram_sticks)
    # Total RAM Count = number of DIMMs; plain summed size like "16GB" (or "1TB" if >= 1000GB)
    ram_dimms = [s for s in ram_sticks if s.get("capacityGB")]
    ram_plain = None
    if ram_sticks:
        # Count every populated slot — a sub-1GB module rounds to
        # capacityGB=0 in the probe and previously vanished from the count.
        f["total_ram_count"] = str(len(ram_sticks))
    if ram_dimms:
        _rg = sum(int(s["capacityGB"]) for s in ram_dimms)
        ram_plain = f"{round(_rg / 1000)}TB" if _rg >= 1000 else f"{_rg}GB"
    elif info.get("ram_gb"):
        _rg = int(info["ram_gb"])
        ram_plain = f"{round(_rg / 1000)}TB" if _rg >= 1000 else f"{_rg}GB"
    if not ram_summary and info.get("ram_gb"):
        ram_summary = f"{int(info['ram_gb'])}GB"
    # Swapped per spec: "Total RAM Size" holds the combined string,
    # "RAM" (ram_summary) holds the plain summed size.
    if ram_summary:
        f["total_ram_size"] = ram_summary
    if ram_plain:
        f["ram_summary"] = ram_plain
    f["sub_category"] = sub
    f["device_type"] = sub
    prim = (ssd or hdd or disks)
    if prim:
        psz = _snap_capacity(prim[0].get("sizeGB"))
        if psz:
            f["storage_gb"] = psz
        f["storage_type"] = "SSD" if ssd else ("HDD" if hdd else "SSD")
    if ssd and hdd:
        hsz = _snap_capacity(hdd[0].get("sizeGB"))
        if hsz:
            f["hdd_capacity_gb"] = hsz
    hdd_summary = _format_hdd_summary(disks)
    # Total Hard Drive Count = # physical disks; plain summed size like "512GB"
    hdd_disks = [d for d in disks if d.get("sizeGB")]
    hdd_plain = None
    if hdd_disks:
        f["total_hdd_count"] = str(len(hdd_disks))
        tot_gb = sum(int(d["sizeGB"]) for d in hdd_disks)
        hdd_plain = f"{round(tot_gb / 1000)}TB" if tot_gb >= 1000 else f"{tot_gb}GB"
    # Swapped per spec: "Total Hard Drive Size" holds the combined string,
    # "Hard Drive" (hdd_summary) holds the plain summed size.
    if hdd_summary:
        f["total_hdd_size"] = hdd_summary
    if hdd_plain:
        f["hdd_summary"] = hdd_plain
    if info.get("screen_in"):
        f["screen_size"] = str(info["screen_in"])
    bh = info.get("battery_health")
    if isinstance(bh, (int, float)) and bh > 0:
        f["battery_health_pct"] = min(int(round(bh)), 100)

    diag = [f"Auto-diagnosed on {info.get('os', 'host')}."]
    if info.get("cpu"):
        diag.append(f"CPU: {info['cpu']} ({info.get('cores', '?')}C).")
    if info.get("ram_gb"):
        diag.append(f"RAM: {info['ram_gb']} GB.")
    if disks:
        diag.append("Storage: " + ", ".join(
            (f"{d.get('sizeGB', '?')}GB {d.get('type', '')}").strip() for d in disks) + ".")
    if info.get("gpu"):
        diag.append(f"GPU: {info['gpu']}.")
    if info.get("has_battery"):
        b = f"Battery: {info.get('battery_pct', '?')}% charge"
        if f.get("battery_health_pct") is not None:
            b += f", design health ~{f['battery_health_pct']}%"
        diag.append(b + ".")
    summary = " ".join(diag)
    f["notes"] = summary
    f["_summary"] = summary
    return f, None


@router.get("/diagnose")
async def diagnose_device(current_user: User = Depends(allowed)):
    """Detect the host machine's hardware (WMI/CIM) and return values mapped to
    the IQC form fields. Detects the machine running the web app."""
    import asyncio
    fields, err = await asyncio.to_thread(_detect_host_hardware)
    if err:
        return JSONResponse({"ok": False, "error": err})
    summary = fields.pop("_summary", "")
    return JSONResponse({"ok": True, "data": fields, "summary": summary})


@router.get("/agent-exe")
async def agent_exe_public():
    """UNAUTHENTICATED agent download for PXE station bootstrap scripts.

    The PXE first-boot .bat (oxyqc-agent/pxe/setup_station.bat) runs before any
    user can log in, so it cannot pass a session cookie. The exe is a hardware
    diagnostic tool with no secrets, and the server is LAN-only post-cutover —
    skipping auth here is deliberate and scoped to this one binary."""
    import os
    from fastapi.responses import FileResponse
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "downloads")
    path = os.path.join(base, "Diagnose_Device_Agent.exe")
    if not os.path.exists(path):
        raise HTTPException(404, "Agent exe not packaged on this server")
    return FileResponse(path, filename="Diagnose_Device_Agent.exe", media_type="application/octet-stream")


@router.get("/agent-installer")
async def agent_installer(current_user: User = Depends(allowed)):
    """Download the single self-installing Diagnose_Device_Agent exe. Running it
    once (no admin) copies it to %LOCALAPPDATA%, registers per-user autostart, and
    starts serving — so the 'Diagnose this Device' button works from then on."""
    import os
    from fastapi.responses import FileResponse
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "downloads")
    path = os.path.join(base, "Diagnose_Device_Agent.exe")
    if not os.path.exists(path):
        # fall back to the legacy name if the renamed build isn't deployed yet
        legacy = os.path.join(base, "OxyQC_Agent.exe")
        if os.path.exists(legacy):
            path = legacy
        else:
            raise HTTPException(404, "Agent exe not packaged on this server")
    return FileResponse(path, filename="Diagnose_Device_Agent.exe", media_type="application/octet-stream")


@router.get("/agent-installer-mac")
async def agent_installer_mac(current_user: User = Depends(allowed)):
    """Download the Diagnose_Device_Agent for macOS as a .command launcher,
    packaged inside a .zip.

    Why a zip: a bare .command served over HTTP always arrives on the Mac
    WITHOUT the executable bit — browsers have no concept of Unix file
    permissions, so there is nothing to preserve. Finder cannot double-click
    -run a non-executable file, so the old bare-file download silently
    stopped working (this is an OS/browser-side behavior, not a one-time
    regression in this file's content — it's why a "previously working"
    .command can stop working with no code change at all). Zipping the file
    with its executable bit set in the archive's own metadata means macOS's
    built-in Archive Utility restores +x on extract, so double-click-to-run
    works again without asking the user to run `chmod +x` by hand.

    If a compiled binary has been built ON a Mac (via PyInstaller — cross-
    compiling to macOS from Windows/Linux isn't possible) and dropped into
    downloads/Diagnose_Device_Agent_mac (no extension, a native executable),
    zip that instead of generating a source launcher."""
    import os
    import io
    import zipfile
    from fastapi.responses import StreamingResponse

    repo_root = os.path.dirname(os.path.dirname(__file__))
    base = os.path.join(repo_root, "downloads")
    prebuilt = os.path.join(base, "Diagnose_Device_Agent_mac")

    def _zip_with_exec_bit(inner_filename: str, content: bytes) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo(inner_filename)
            info.date_time = _dtnow.now().timetuple()[:6]
            # High 16 bits of external_attr hold the Unix mode; 0o755 = rwxr-xr-x
            info.external_attr = (0o755 & 0xFFFF) << 16
            zf.writestr(info, content)
        return buf.getvalue()

    if os.path.exists(prebuilt):
        with open(prebuilt, "rb") as fh:
            payload = _zip_with_exec_bit("Diagnose_Device_Agent.command", fh.read())
        return Response(
            content=payload, media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=Diagnose_Device_Agent_mac.zip"},
        )

    # Source-only fallback (works immediately, no compiled build required):
    # the agent script bundled inside this repo — same file used to build the
    # Windows exe, kept cross-platform-capable (see oxyqc_agent.py detect()).
    agent_src_path = os.path.join(repo_root, "oxyqc-agent", "oxyqc_agent.py")
    if not os.path.exists(agent_src_path):
        raise HTTPException(404, "Agent source not found on this server")

    with open(agent_src_path, "r", encoding="utf-8") as fh:
        agent_source = fh.read()

    # Single self-contained launcher: writes the embedded source to a local
    # support-file path (quoted heredoc — no shell expansion of the embedded
    # PowerShell-lookalike text/$-signs inside oxyqc_agent.py), then runs it.
    command_file = f"""#!/bin/bash
# Diagnose_Device_Agent launcher (macOS) — double-click to run.
# Self-contained: no separate .py file to manage. First run writes the agent
# to ~/Library/Application Support and registers a per-user LaunchAgent (no
# sudo). After that, it starts automatically at login.
set -e
PY=$(command -v python3 || echo "")
if [ -z "$PY" ]; then
  osascript -e 'display alert "Python 3 not found" message "Install it from python.org or run: xcode-select --install, then double-click this file again."'
  exit 1
fi
SUPPORT_DIR="$HOME/Library/Application Support/Diagnose_Device_Agent"
mkdir -p "$SUPPORT_DIR"
cat <<'OXYQC_AGENT_PY_EOF' > "$SUPPORT_DIR/oxyqc_agent.py"
{agent_source}
OXYQC_AGENT_PY_EOF
"$PY" "$SUPPORT_DIR/oxyqc_agent.py"
"""

    payload = _zip_with_exec_bit("Diagnose_Device_Agent.command", command_file.encode("utf-8"))
    return Response(
        content=payload, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=Diagnose_Device_Agent_mac.zip"},
    )


@router.get("/usb-import")
async def usb_import(current_user: User = Depends(allowed)):
    """Auto-pick the latest IQC data file from a connected OxyQC USB drive and
    return the saved payload (used by the IQC form to prefill all fields)."""
    import json as _json
    f = _find_usb_iqc_file()
    if not f:
        raise HTTPException(404, "No OxyQC USB data file found. Plug in the OxyQC USB drive.")
    try:
        data = _json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(422, f"Could not read USB data file: {e}")
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return {"source": str(f), "data": data}


def _iqc_filters(stage, device_type, grade, lot, q, date_from, date_to):
    """Filter clauses shared by the Product IQC page and its data endpoint."""
    from utils.date_filter import apply_date_range
    w = [Device.is_active.is_(True), Device.is_trashed == False]
    if stage:
        try:
            w.append(Device.current_stage == DeviceStage(stage))
        except ValueError:
            w.append(Device.current_stage == DeviceStage.iqc)
    else:
        w.append(Device.current_stage == DeviceStage.iqc)
    if device_type:
        w.append(Device.device_type == device_type)
    if grade:
        w.append(Device.grade == grade)
    if lot:
        w.append(Lot.lot_number == lot)
    if q:
        q_like = f"%{q}%"
        w.append(or_(Device.barcode.ilike(q_like), Device.brand.ilike(q_like),
                     Device.model.ilike(q_like), Device.serial_no.ilike(q_like)))
    apply_date_range(w, Device.created_at, date_from, date_to)
    return w


@router.get("/data")
async def iqc_list_data(
    request: Request,
    draw: int = 1, start: int = 0, length: int = 25,
    q: str = "", stage: str = "", grade: str = "", lot: str = "",
    device_type: str = "", date_from: str = "", date_to: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DataTables server-side feed for Product IQC.

    Same fix as Inventory Search: at ~3,900 devices this table rendered every
    matching row into the HTML on every load. Column layout mirrors the
    template exactly, including Floor/Added staying present but hidden via
    DataTables' own columnDefs (visible:false) — that mechanism hides header
    and cell together and was already correct here, unlike the d-none class
    that caused a misalignment bug on the Sales list.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from html import escape

    page_filters = _iqc_filters(stage, device_type, grade, lot, q, date_from, date_to)
    base = select(Device, Lot.lot_number).join(Lot, Device.lot_id == Lot.id).where(*page_filters)
    count_base = select(func.count()).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(*page_filters)

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = []
    if search:
        like = f"%{search}%"
        search_filters.append(or_(
            Device.barcode.ilike(like), Device.serial_no.ilike(like), Device.brand.ilike(like),
            Device.model.ilike(like), Device.cpu.ilike(like), Lot.lot_number.ilike(like),
        ))

    total = (await db.execute(count_base)).scalar() or 0
    # No search term means the two counts are the same query; DataTables asks on
    # every draw (paging, sorting, page-size change), so only pay for the second
    # when a search term actually narrows the set.
    filtered = total if not search_filters else (
        (await db.execute(count_base.where(*search_filters))).scalar() or 0)

    col_map = {1: Device.serial_no, 2: Device.barcode, 3: Lot.lot_number,
               4: Device.current_stage, 5: Device.brand, 6: Device.model,
               7: Device.device_type, 8: Device.cpu, 11: Device.grade,
               12: Device.floor, 13: Device.created_at}
    try:
        order_col = int(request.query_params.get("order[0][column]", 13))
    except ValueError:
        order_col = 13
    order_dir = request.query_params.get("order[0][dir]", "desc")
    sort_expr = col_map.get(order_col, Device.created_at)
    order_by = _asc(sort_expr) if order_dir == "asc" else _desc(sort_expr)

    rows = (await db.execute(
        base.where(*search_filters).order_by(order_by, Device.barcode)
        .offset(max(0, start)).limit(min(max(1, length), 5000))
    )).all()

    def esc(v):
        return escape(str(v)) if v is not None else ""

    data = []
    for d, lot_number in rows:
        stage_val = getattr(d.current_stage, "value", d.current_stage)
        g = getattr(d.grade, "value", d.grade) if d.grade else None
        gcls = "success" if g == "A" else "warning" if g == "B" else "danger"
        ram = d.ram_summary or (f"{d.ram_gb}GB" if d.ram_gb else "—")
        storage = d.hdd_summary or (f"{d.storage_gb}GB {d.storage_type or ''}" if d.storage_gb else "—")
        data.append([
            f'<input type="checkbox" class="form-check-input iqcRowChk" value="{esc(d.barcode)}">',
            f'<span class="font-monospace small">{esc(d.serial_no or "—")}</span>',
            f'<a href="/devices/{esc(d.barcode)}" class="text-decoration-none"><code>{esc(d.barcode)}</code></a>',
            (f'<a href="/devices?lot={esc(lot_number)}" class="text-decoration-none">'
             f'<span class="badge bg-info text-dark">{esc(lot_number)}</span></a>'),
            f'<span class="badge bg-secondary">{esc(STAGE_LABELS.get(d.current_stage, stage_val))}</span>',
            esc(d.brand or "—"), esc(d.model or "—"), esc(d.device_type or "—"),
            f'<span class="text-muted small">{esc(d.cpu or "—")}</span>',
            esc(ram), esc(storage),
            (f'<span class="badge bg-{gcls}">{esc(g)}</span>' if g else "—"),
            esc(d.floor or "—"),
            d.created_at.strftime("%d-%m-%Y") if d.created_at else "—",
            (f'<div class="d-flex gap-1">'
             f'<a href="/devices/{esc(d.barcode)}/edit" class="btn btn-sm btn-outline-primary py-0 px-2" title="Edit"><i class="bi bi-pencil"></i></a>'
             f'<button type="button" class="btn btn-sm btn-outline-danger py-0 px-1 trash-one-btn" data-barcode="{esc(d.barcode)}" title="Move to Trash"><i class="bi bi-trash3"></i></button>'
             f'</div>'),
        ])

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data}


@router.get("", response_class=HTMLResponse)
async def iqc_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    # Open to every signed-in user: the "Add New Asset" button lives on this
    # page, so gating it behind the built-in allow-list blocked roles that do
    # IQC entry day to day (trc_manager among them) from reaching the form.
    current_user: User = Depends(get_current_user),
    q: str = Query(default=""),
    stage: str = Query(default=""),
    grade: str = Query(default=""),
    lot: str = Query(default=""),
    device_type: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
):
    # Stage defaults to IQC (this page's usual purpose) unless the user
    # explicitly picks a different stage from the new Stage filter — lets
    # this page double as a broader device search without changing its
    # default behavior for anyone who doesn't touch the filter. Blank out an
    # invalid stage value the same way the old inline code did.
    if stage:
        try:
            DeviceStage(stage)
        except ValueError:
            stage = ""
    base_filters = _iqc_filters(stage, device_type, grade, lot, q, date_from, date_to)

    # Rows for the main table come from /iqc/data (DataTables server-side); the
    # full fetch this handler used to do — up to ~3,900 devices — existed only
    # to render them and to compute `total`, which a COUNT does far cheaper.
    count_q = select(func.count()).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(*base_filters)
    total = (await db.execute(count_q)).scalar() or 0
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()
    # Product Lots table — same rows and counts as Lot Management, so the two
    # pages can never disagree about how many devices a lot has registered.
    from utils.lot_helpers import build_lot_stats
    product_lots = await db.execute(
        select(Lot).where(Lot.is_trashed.isnot(True)).order_by(Lot.created_at.desc())
    )
    lot_stats = await build_lot_stats(db, product_lots.scalars().all())
    # Model Based Summary — same builder used on Overall Inventory, scoped to
    # this page's currently-filtered device set (reuses base_filters as-is).
    model_summary = await _build_model_summary(db, base_filters)
    return templates.TemplateResponse("iqc/list.html", {
        "request": request, "lots": lots, "current_user": current_user,
        "total": total,
        "q": q, "stage": stage, "grade": grade, "lot": lot,
        "device_type": device_type, "device_type_options": await master_values(db, "device_type"),
        "stage_options": [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES],
        "stage_labels": STAGE_LABELS,
        "model_summary": model_summary,
        "lot_stats": lot_stats,
    })


@router.post("/create-lot-from-selection")
async def iqc_create_lot_from_selection(
    request: Request,
    barcodes: list[str] = Form(...),
    lot_number: str = Form(...),
    purchase_date: str = Form(...),
    supplier_name: str = Form(...),
    grn_date: str = Form(""),
    vendor_name: str = Form(""),
    condition: str = Form(""),
    buying_price: str = Form("0"),
    selling_price: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("iqc", "add")),
):
    """Create a new Lot from a set of Tag Numbers selected on the IQC Line
    Item table, and re-assign those devices to the new lot."""
    barcodes = [b.strip() for b in barcodes if b and b.strip()]
    if not barcodes:
        return RedirectResponse(url="/iqc?error=No+devices+selected", status_code=302)

    existing = await db.execute(select(Lot).where(Lot.lot_number == lot_number.strip()))
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/iqc?error=Lot+Number+{lot_number.strip()}+already+exists", status_code=302)

    try:
        purchase_dt = _dtnow.strptime(purchase_date, "%Y-%m-%d")
    except ValueError:
        # Fall back to "now" in the app timezone, not UTC. utcnow() here filed a
        # lot entered before 05:30 IST under the PREVIOUS day's purchase date.
        purchase_dt = app_now()
    grn_dt = None
    if grn_date:
        try:
            grn_dt = _dtnow.strptime(grn_date, "%Y-%m-%d")
        except ValueError:
            grn_dt = None

    lot = Lot(
        lot_number=lot_number.strip(),
        supplier_name=supplier_name.strip(),
        purchase_date=purchase_dt,
        grn_date=grn_dt,
        vendor_name=vendor_name.strip() or None,
        qty=len(barcodes),
        condition=condition.strip() or None,
        buying_price=float(buying_price) if (buying_price or "").strip() else 0,
        selling_price=float(selling_price) if (selling_price or "").strip() else None,
        notes=notes.strip() or None,
        created_by=current_user.username if current_user else None,
    )
    db.add(lot)
    await db.flush()

    result = await db.execute(select(Device).where(Device.barcode.in_(barcodes)))
    devices = result.scalars().all()
    for device in devices:
        device.lot_id = lot.id

    await audit(db, action="LOT_CREATED_FROM_IQC_SELECTION", user=current_user,
                table_name="lots", record_id=str(lot.id),
                new_value={"lot_number": lot.lot_number, "barcodes": barcodes}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/iqc?success=Lot+{lot.lot_number}+created+with+{len(devices)}+device(s)", status_code=302)


_CUSTOMISE_RETURN_PATHS = {"/iqc", "/devices", "/stock"}


@router.post("/bulk-apply-grade-type")
async def iqc_bulk_apply_grade_type(
    request: Request,
    barcodes: list[str] = Form(default=[]),
    lot_numbers: list[str] = Form(default=[]),
    model_keys: list[str] = Form(default=[]),
    return_to: str = Form(default="/iqc"),
    device_type: str = Form(""),
    entity: str = Form(""),
    grade: str = Form(""),
    invoice_number: str = Form(""),
    po_number: str = Form(""),
    grn_number: str = Form(""),
    location_id: str = Form(""),
    device_price: str = Form(""),
    bin_location: str = Form(""),
    min_selling_price: str = Form(""),
    cpu: str = Form(""),
    cpu_make: str = Form(""),
    generation: str = Form(""),
    ram_gb: str = Form(""),
    storage_gb: str = Form(""),
    total_ram_count: str = Form(""),
    total_ram_size: str = Form(""),
    total_hdd_count: str = Form(""),
    total_hdd_size: str = Form(""),
    hdd_summary: str = Form(""),
    touch_type: str = Form(""),
    stock_type: str = Form(""),
    sub_lot_number: str = Form(""),
    to_stage: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    # Historically also posted from Product IQC — that modal was removed, but
    # the endpoint still lives under /iqc and accepts either module's edit
    # right so both All Inventory and Inventory Manager can use it.
    _perm: User = Depends(require_any_module_perm("iqc", "devices", action="edit")),
    # The check above only ever looks at each module's "enable" bit (see
    # has_perm()'s docstring — the matrix's own Edit checkbox is display-only)
    # so it never actually blocked anyone. This is the real, enforced gate —
    # Devices Edit or IQC Edit from the Role Additional Permissions tab.
    _perm2: User = Depends(require_any_additional_perm("edit_devices", "edit_iqc")),
):
    """Bulk-apply Device Type, Entity, Grade, Invoice Number, GRN Number,
    Location ID, Device Price, Min Selling Price and/or a stage move to a set
    of devices. Powers
    the shared Customise modal (_customise_modal.html) on Overall Inventory's
    Tag Number + Lot Based Summary tables (barcodes[] or lot_numbers[]) and
    the Model Based Summary tables on both All Inventory and Inventory
    Manager (model_keys[], each "model|||brand" — resolved to every device in
    that group). The stage move reuses the same validated-transition engine
    as every other move-device flow in the app (services/control_engine) — a
    barcode whose current stage has no allowed transition to the requested
    stage is skipped and reported, not silently dropped or force-moved.

    `bin_location` (2026-10-16) is free text saved as each selected tag's own
    Bin Location (Device.bin_location), shown under Location ID. It does not
    need or change the Location ID."""
    return_to = return_to if return_to in _CUSTOMISE_RETURN_PATHS else "/iqc"

    barcode_set = {b.strip() for b in barcodes if b and b.strip()}

    if lot_numbers:
        lot_nums = [l.strip() for l in lot_numbers if l and l.strip()]
        if lot_nums:
            lot_ids = (await db.execute(select(Lot.id).where(Lot.lot_number.in_(lot_nums)))).scalars().all()
            if lot_ids:
                lot_barcodes = (await db.execute(
                    select(Device.barcode).where(Device.lot_id.in_(lot_ids))
                )).scalars().all()
                barcode_set.update(lot_barcodes)

    if model_keys:
        model_filters = []
        for key in model_keys:
            if "|||" not in key:
                continue
            model_val, brand_val = key.split("|||", 1)
            model_filters.append(and_(Device.model == model_val, Device.brand == brand_val))
        if model_filters:
            model_barcodes = (await db.execute(
                select(Device.barcode).where(or_(*model_filters))
            )).scalars().all()
            barcode_set.update(model_barcodes)

    barcodes = sorted(barcode_set)
    if not barcodes:
        return RedirectResponse(url=f"{return_to}?error=No+devices+selected", status_code=302)
    to_stage = (to_stage or "").strip()
    grn_number = grn_number.strip()
    location_id = location_id.strip()
    device_price = device_price.strip()
    bin_location = bin_location.strip()
    if len(bin_location) > 50:
        return RedirectResponse(url=f"{return_to}?error=Bin+Location+is+too+long+(max+50)", status_code=302)
    min_selling_price = min_selling_price.strip()
    if (not device_type.strip() and not entity.strip() and not grade.strip() and not invoice_number.strip()
            and not po_number.strip() and not grn_number and not location_id and not device_price
            and not min_selling_price
            and not cpu.strip() and not cpu_make.strip()
            and not generation.strip() and not ram_gb.strip() and not storage_gb.strip()
            and not total_ram_count.strip() and not total_ram_size.strip()
            and not total_hdd_count.strip() and not total_hdd_size.strip()
            and not hdd_summary.strip() and not touch_type.strip() and not to_stage
            and not stock_type.strip() and not bin_location):
        return RedirectResponse(url=f"{return_to}?error=Select+a+field+to+apply", status_code=302)

    result = await db.execute(select(Device).where(Device.barcode.in_(barcodes)))
    devices = result.scalars().all()

    new_stage = None
    skipped_moves = []
    if to_stage:
        try:
            new_stage = DeviceStage(to_stage)
        except ValueError:
            return RedirectResponse(url=f"{return_to}?error=Invalid+stage+{to_stage}", status_code=302)
        # Sold is never a Customise target (not in the dropdown either): a sale
        # must go through New Sale so a Sale record exists for the tag.
        if new_stage == DeviceStage.sold:
            return RedirectResponse(url=f"{return_to}?error=Use+New+Sale+to+mark+tags+Sold", status_code=302)

    # Grade is a Postgres enum, so an unrecognised string would only fail at
    # commit — after the loop below has already mutated every selected device.
    # Reject up front instead, the same way an invalid stage is rejected.
    new_grade = None
    if grade.strip():
        new_grade = parse_grade(grade)
        if new_grade is None:
            return RedirectResponse(url=f"{return_to}?error=Invalid+grade+{grade.strip()}",
                                    status_code=302)

    # device_price is a Numeric column — reject a non-numeric value up front
    # rather than failing at commit after every selected device was mutated.
    new_device_price = None
    if device_price:
        try:
            new_device_price = Decimal(device_price)
        except InvalidOperation:
            return RedirectResponse(url=f"{return_to}?error=Invalid+device+price+{device_price}",
                                    status_code=302)

    # min_selling_price is a Numeric column — same up-front validation as device_price.
    new_min_selling_price = None
    if min_selling_price:
        try:
            new_min_selling_price = Decimal(min_selling_price)
        except InvalidOperation:
            return RedirectResponse(url=f"{return_to}?error=Invalid+min+selling+price+{min_selling_price}",
                                    status_code=302)

    # location_id is a StorageLocation FK — validate it resolves to a real,
    # active location before touching any device.
    new_location = None
    if location_id:
        loc_result = await db.execute(
            select(StorageLocation).where(StorageLocation.id == location_id)
        )
        new_location = loc_result.scalar_one_or_none()
        if new_location is None:
            return RedirectResponse(url=f"{return_to}?error=Invalid+location", status_code=302)


    # Stock Type (2026-10-15): As-Is Lot sets the typed Sub-Lot Number;
    # Finish Good / In Progress clear it, after which the tag's type follows
    # its stage (routers/devices.stock_type_key). Validated before any write.
    stock_type = (stock_type or "").strip()
    new_sub_lot = (sub_lot_number or "").strip()
    if stock_type and stock_type not in ("as_is", "finish_good", "in_progress"):
        return RedirectResponse(url=f"{return_to}?error=Invalid+stock+type", status_code=302)
    if stock_type == "as_is" and not new_sub_lot:
        return RedirectResponse(url=f"{return_to}?error=Enter+a+Sub-Lot+Number+for+As-Is+Lot", status_code=302)
    if len(new_sub_lot) > 50:
        return RedirectResponse(url=f"{return_to}?error=Sub-Lot+Number+is+too+long+(max+50)", status_code=302)

    # Touch Type: "touch" / "non_touch" (blank = leave unchanged). Anything
    # else is rejected before any device is touched.
    new_touch_type = touch_type.strip().lower() or None
    if new_touch_type and new_touch_type not in ("touch", "non_touch"):
        return RedirectResponse(url=f"{return_to}?error=Invalid+touch+type", status_code=302)

    # Stage moves skip the allowed-transitions table for admin — and, from the
    # Customise modal on All Inventory (/devices) and Inventory Manager
    # (/stock), for every user who can open it (2026-10-15: any stage to any
    # stage, no admin gate). The modal itself still needs Devices Edit / IQC
    # Edit (_perm2 above).
    is_admin = current_user.role.value == "admin"
    override_transitions = is_admin or return_to in ("/devices", "/stock")
    for device in devices:
        if stock_type == "as_is":
            device.sub_lot_number = new_sub_lot
        elif stock_type in ("finish_good", "in_progress"):
            device.sub_lot_number = None
        if new_touch_type:
            device.touch_type = new_touch_type
        if device_type.strip():
            device.device_type = device_type.strip()
        if entity.strip():
            device.entity = entity.strip()
        if new_grade is not None:
            device.grade = new_grade
        if invoice_number.strip():
            device.invoice_number = invoice_number.strip()
        if po_number.strip():
            device.po_number = po_number.strip()
        if grn_number:
            device.grn_number = grn_number
        if new_device_price is not None:
            device.device_price = new_device_price
        if new_min_selling_price is not None:
            device.min_selling_price = new_min_selling_price
        if bin_location:
            device.bin_location = bin_location
        if new_location is not None:
            db.add(DeviceLocationLog(
                device_id=device.id, location_id=new_location.id,
                action=LocationAction.assigned, actor_id=current_user.id,
                actor_name=current_user.full_name,
                notes="Bulk Customise modal — bulk Location ID",
            ))
        if cpu.strip():
            device.cpu = cpu.strip()
        if cpu_make.strip():
            device.cpu_make = cpu_make.strip()
        if generation.strip():
            device.generation = generation.strip()
        if ram_gb.strip() and ram_gb.strip().isdigit():
            device.ram_gb = int(ram_gb.strip())
        if storage_gb.strip() and storage_gb.strip().isdigit():
            device.storage_gb = int(storage_gb.strip())
        if total_ram_count.strip():
            device.total_ram_count = total_ram_count.strip()
        if total_ram_size.strip():
            device.total_ram_size = total_ram_size.strip()
        if total_hdd_count.strip():
            device.total_hdd_count = total_hdd_count.strip()
        if total_hdd_size.strip():
            device.total_hdd_size = total_hdd_size.strip()
        if hdd_summary.strip():
            device.hdd_summary = hdd_summary.strip()
        if new_stage is not None:
            try:
                await validate_transition(device, new_stage, db, override_admin=override_transitions)
            except HTTPException:
                skipped_moves.append(device.barcode)
                continue
            prev = device.current_stage
            # Into Pass Hold / Ready to Sale without the Final QC Pass button:
            # write verified RAM/Hard Drive changes onto the spec (2026-10-15).
            from services.parts_changed import apply_spec_changes_if_past_final_qc
            await apply_spec_changes_if_past_final_qc(db, device, prev, new_stage, current_user, request)
            device.current_stage = new_stage
            from models.device import reset_repair_cycle_on_entry
            reset_repair_cycle_on_entry(device, prev, new_stage)
            device.updated_at = app_now()
            # A bulk move off L1/L2/L3 bypasses the dedicated Mark-Complete
            # flows, which are otherwise the only places that ever close an
            # open WorkOrder — without this the WorkOrder is orphaned: it
            # keeps showing on /repair/l1, /repair/l3l4, and Production
            # Manager's queue tiles, disagreeing with the device's real (now
            # different) stage forever. Same fix already applied to the
            # dedicated manual-move endpoint in routers/repair.py.
            if prev in (DeviceStage.l1, DeviceStage.l2) and new_stage not in (DeviceStage.l1, DeviceStage.l2):
                # Also matches stage l1/l2 WorkIDs (the plain engineer-pick
                # ones), not just "L1L2-" — those were left open here.
                from routers.repair import _close_l1l2_work_orders
                await _close_l1l2_work_orders(db, device.id)
            elif prev == DeviceStage.l3 and new_stage != DeviceStage.l3:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(WorkOrder)
                    .where(WorkOrder.device_id == device.id, WorkOrder.work_id.like("L3L4-%"),
                           WorkOrder.status != "completed")
                    .values(status="completed", completed_at=app_now())
                )
            db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=new_stage,
                                  moved_by=current_user.username, notes="Bulk Customise modal — bulk Move to Stage"))

    await audit(db, action="IQC_BULK_GRADE_TYPE_APPLIED", user=current_user,
                table_name="devices", record_id=",".join(str(d.id) for d in devices)[:50],
                new_value={"device_type": device_type or None, "grade": grade or None,
                           "invoice_number": invoice_number or None, "po_number": po_number or None,
                           "grn_number": grn_number or None, "location_id": location_id or None,
                           "device_price": device_price or None,
                           "min_selling_price": min_selling_price or None,
                           "touch_type": new_touch_type,
                           "stock_type": stock_type or None,
                           "sub_lot_number": new_sub_lot if stock_type == "as_is" else None,
                           "bin_location": bin_location or None,
                           "cpu": cpu or None, "cpu_make": cpu_make or None,
                           "generation": generation or None,
                           "ram_gb": ram_gb or None, "storage_gb": storage_gb or None,
                           "total_ram_count": total_ram_count or None, "total_ram_size": total_ram_size or None,
                           "total_hdd_count": total_hdd_count or None, "total_hdd_size": total_hdd_size or None,
                           "hdd_summary": hdd_summary or None,
                           "to_stage": to_stage or None, "count": len(devices)},
                request=request)
    await db.commit()
    msg = f"{len(devices)}+device(s)+updated"
    if skipped_moves:
        import urllib.parse
        msg += f"&warning={urllib.parse.quote(f'{len(skipped_moves)} tag(s) could not move to {to_stage} (not an allowed transition): ' + ', '.join(skipped_moves[:10]))}"
    return RedirectResponse(url=f"{return_to}?success={msg}", status_code=302)


# Device columns the export leads with, in the order they appear in the file.
# Everything the inspection itself records is appended after these, straight
# from the IQCInspection model, so a new inspection field shows up in the export
# without anyone remembering to add it here.
_EXPORT_DEVICE_FIELDS = [
    "barcode", "serial_no", "entity", "grade", "brand", "model", "device_type",
    "sub_category", "cpu", "cpu_make", "generation",
    "ram_summary", "total_ram_count", "total_ram_size", "ram_gb",
    "hdd_summary", "total_hdd_count", "total_hdd_size", "storage_gb", "storage_type",
    "screen_size", "battery_health_pct", "color", "bios_password",
    "grn_number", "invoice_number", "po_number", "floor", "warehouse",
    "qty", "device_price", "notes",
]
_EXPORT_INSPECTION_FIELDS = [
    c.name for c in IQCInspection.__table__.columns
    if c.name not in ("id", "device_id")
]


@router.get("/export-csv")
async def iqc_export_csv(
    request: Request,
    q: str = "", stage: str = "", grade: str = "", lot: str = "",
    device_type: str = "", date_from: str = "", date_to: str = "",
    barcodes: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Export IQC devices with their full inspection record.

    Two entry points share this route. "Export All" sends the page's current
    filters so the file matches what is on screen; "Export Selected" sends a
    comma-separated `barcodes` list and ignores the filters, since the user
    picked those rows explicitly. Every column of IQCInspection is included —
    grade and stage and entity from the device, then hardware, body and
    cosmetic straight from the inspection.
    """
    picked = [b.strip() for b in barcodes.split(",") if b.strip()]
    if picked:
        filters = [Device.barcode.in_(picked)]
    else:
        filters = _iqc_filters(stage, device_type, grade, lot, q, date_from, date_to)

    result = await db.execute(
        select(Device, Lot.lot_number, IQCInspection)
        .join(Lot, Device.lot_id == Lot.id)
        .outerjoin(IQCInspection, IQCInspection.device_id == Device.id)
        .where(*filters)
        .order_by(Device.created_at.desc())
    )
    rows = result.all()

    def _cell(v):
        if v is None:
            return ""
        v = getattr(v, "value", v)          # enums -> their value
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M") if getattr(v, "hour", None) is not None \
                else v.strftime("%Y-%m-%d")
        return v

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["lot_number", "stage"] + _EXPORT_DEVICE_FIELDS
        + ["created_at", "updated_at"] + _EXPORT_INSPECTION_FIELDS
    )
    for device, lot_number, insp in rows:
        writer.writerow(
            [lot_number or "", _cell(device.current_stage)]
            + [_cell(getattr(device, f, None)) for f in _EXPORT_DEVICE_FIELDS]
            + [_cell(device.created_at), _cell(device.updated_at)]
            + [_cell(getattr(insp, f, None)) if insp else "" for f in _EXPORT_INSPECTION_FIELDS]
        )
    suffix = "selected" if picked else "all"
    filename = f"iqc-{suffix}-{app_now().strftime('%Y%m%d-%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/new", response_class=HTMLResponse)
async def iqc_new_form(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(get_current_user),
                       lot_id: str = Query(default=""), grn_number: str = Query(default=""),
                       error: str = Query(default="")):
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()
    return templates.TemplateResponse("iqc/form.html", {
        # `error` also arrives as a query param when a save failed and we
        # redirected back here — surface it instead of silently dropping it.
        "request": request, "lots": lots, "current_user": current_user, "error": error or None,
        "prefill_lot_id": lot_id, "prefill_grn": grn_number,
    })


@router.post("/new")
@_iqc_form_boundary
async def iqc_create(
    request: Request,
    barcode: str = Form(...),
    entity: str = Form(""),
    lot_id: str = Form(""),
    sub_category: str = Form(""),
    device_type: str = Form(""),
    brand: str = Form(""),
    model: str = Form(""),
    serial_no: str = Form(""),
    grn_number: str = Form(""),
    cpu: str = Form(""),
    cpu_make: str = Form(""),
    generation: str = Form(""),
    ram_summary: str = Form(""),
    hdd_summary: str = Form(""),
    total_ram_count: str = Form(""),
    total_ram_size: str = Form(""),
    total_hdd_count: str = Form(""),
    total_hdd_size: str = Form(""),
    screen_size: str = Form(""),
    battery_health_pct: str = Form(""),
    bios_password: str = Form(""),
    color: str = Form(""),
    invoice_number: str = Form(""),
    grade: str = Form(""),
    floor: str = Form(""),
    warehouse: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    lot_line_item_id: str = Form(""),
    qty: str = Form(""),
    device_price_input: str = Form(""),  # manual override field
    # ── Functional status ────────────────────────────────────────────────────
    power_on: str = Form(""),
    status: str = Form(""),
    all_ok: str = Form(""),
    r2v3_grade_category: str = Form(""),
    # ── Screen condition ─────────────────────────────────────────────────────
    screen_dot: str = Form(""),
    screen_line: str = Form(""),
    screen_functional: str = Form(""),
    screen_discoloration: str = Form(""),
    screen_patch: str = Form(""),
    touch_screen: str = Form(""),
    screen_broken: str = Form(""),
    screen_flickering: str = Form(""),
    screen_scratch: str = Form(""),
    screen_loose: str = Form(""),
    screen_missing: str = Form(""),
    screen_hinge_broken: str = Form(""),
    screen_colour_spread: str = Form(""),
    screen_keyboard_mark: str = Form(""),
    screen_hard_press: str = Form(""),
    # ── Panel A ──────────────────────────────────────────────────────────────
    panel_a_scratch: str = Form(""),
    panel_a_broken: str = Form(""),
    panel_a_missing: str = Form(""),
    panel_a_dent: str = Form(""),
    panel_a_colour_fade: str = Form(""),
    # ── Panel B ──────────────────────────────────────────────────────────────
    panel_b_scratch: str = Form(""),
    panel_b_colour_fade: str = Form(""),
    panel_b_rubber_cut: str = Form(""),
    panel_b_broken: str = Form(""),
    panel_b_missing: str = Form(""),
    # ── Panel C ──────────────────────────────────────────────────────────────
    panel_c_scratch: str = Form(""),
    panel_c_broken: str = Form(""),
    panel_c_missing: str = Form(""),
    panel_c_dent: str = Form(""),
    panel_c_colour_fade: str = Form(""),
    # ── Panel D ──────────────────────────────────────────────────────────────
    panel_d_dent: str = Form(""),
    panel_d_colour_fade: str = Form(""),
    panel_d_scratch: str = Form(""),
    panel_d_broken: str = Form(""),
    panel_d_missing: str = Form(""),
    # ── Keyboard ─────────────────────────────────────────────────────────────
    keyboard_working: str = Form(""),
    keyboard_colour_fade: str = Form(""),
    keyboard_key_missing: str = Form(""),
    keyboard_hard_press: str = Form(""),
    # ── Speaker ──────────────────────────────────────────────────────────────
    speaker_status: str = Form(""),
    # ── Touchpad ─────────────────────────────────────────────────────────────
    touchpad_working: str = Form(""),
    touchpad_click_working: str = Form(""),
    touchpad_scratch: str = Form(""),
    touchpad_colour_fade: str = Form(""),
    touchpad_missing: str = Form(""),
    # ── Ports ────────────────────────────────────────────────────────────────
    port_hdmi: str = Form(""),
    port_usb_working: str = Form(""),
    port_audio_jack: str = Form(""),
    usb_a_ports: str = Form(""),
    usb_c_ports: str = Form(""),
    ethernet_ports: str = Form(""),
    # ── Other components ─────────────────────────────────────────────────────
    wifi_status: str = Form(""),
    webcam_status: str = Form(""),
    hdd_connector: str = Form(""),
    hdd_casing: str = Form(""),
    battery_present: str = Form(""),
    battery_cable: str = Form(""),
    charging_port: str = Form(""),
    dvd_drive: str = Form(""),
    # ── Covers and Casing ────────────────────────────────────────────────────
    cover_ram: str = Form(""),
    cover_dvd: str = Form(""),
    cover_storage: str = Form(""),
    # ── Hinge ────────────────────────────────────────────────────────────────
    hinge_condition: str = Form(""),
    hinge_cover: str = Form(""),
    touchpad_logicboard: str = Form(""),
    # ── Storage / Fan ────────────────────────────────────────────────────────
    storage_health_pct: str = Form(""),
    fan_sound_dba: str = Form(""),
    fan_working: str = Form(""),
    db: AsyncSession = Depends(get_db),
    # IQC entry is open to every signed-in user: any role that physically
    # receives a device must be able to register it, and gating this behind the
    # built-in allow-list plus the matrix's "add" bit was locking out roles
    # (trc_manager among them) that do this work day to day.
    current_user: User = Depends(get_current_user),
    # Add IQC (Role Additional Permissions tab) — defaults to permitted for
    # every role (same "permissive until an admin opts out" convention as
    # everywhere else), so this changes nothing for the history above unless
    # an admin explicitly unchecks it for a specific role.
    _perm: User = Depends(require_additional_perm("add_iqc")),
):
    # Case-folded: a tag scanned or typed with different capitalisation than
    # however it was first entered used to pass this check clean and register
    # as a second, fully independent device for the same physical unit. The
    # barcode column's UNIQUE constraint is case-sensitive at the database
    # level, so nothing there caught it either.
    existing = await db.execute(select(Device).where(func.upper(Device.barcode) == barcode.upper()))
    if existing.scalar_one_or_none():
        lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
        lots = lots_result.scalars().all()
        return templates.TemplateResponse("iqc/form.html", {
            "request": request, "lots": lots, "current_user": current_user,
            "error": f"Barcode {barcode} already exists"
        })

    # ── Validate UUID inputs ─────────────────────────────────────────────────
    # A non-UUID lot_id / lot_line_item_id (e.g. from a stale barcode autofill or
    # an unselected dropdown) would otherwise hit Postgres' "invalid input syntax
    # for type uuid" and get masked as a misleading 404. Fail cleanly instead.
    import uuid as _uuid

    def _is_uuid(v):
        try:
            _uuid.UUID(str(v))
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    if not (lot_id or "").strip():
        from utils.lot_helpers import get_or_create_unassigned_lot
        try:
            unassigned = await get_or_create_unassigned_lot(db)
            lot_id = str(unassigned.id)
        except Exception as exc:
            # Belt-and-suspenders on top of get_or_create_unassigned_lot's own
            # race handling — any other failure resolving the fallback Lot
            # (not just the known unique-constraint race) still degrades to a
            # clean form error instead of an unhandled 500.
            _keep_after_rollback(db, current_user)  # before rollback — see _iqc_form_boundary's wrapper for why
            await db.rollback()
            lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
            lots = lots_result.scalars().all()
            return templates.TemplateResponse("iqc/form.html", {
                "request": request, "lots": lots, "current_user": current_user,
                "error": f"Could not resolve a Lot for this device: {str(exc)[:180]}",
            })
    elif not _is_uuid(lot_id):
        lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
        lots = lots_result.scalars().all()
        return templates.TemplateResponse("iqc/form.html", {
            "request": request, "lots": lots, "current_user": current_user,
            "error": "Please select a valid Lot from the dropdown before adding to IQC.",
        })
    # Ignore a malformed line-item id rather than crashing the insert
    if lot_line_item_id and not _is_uuid(lot_line_item_id):
        lot_line_item_id = ""

    # ── Resolve Location ID -> StorageLocation (Floor/Zone -> Location ID cascade). ──
    # warehouse is legacy free-text; best-effort mirror the resolved location's
    # display_name into it for backward-compat display in older reports/exports.
    resolved_location_id = None
    resolved_warehouse = warehouse or None
    if location_id and _is_uuid(location_id):
        loc_result = await db.execute(
            select(StorageLocation).where(StorageLocation.id == location_id)
        )
        loc = loc_result.scalar_one_or_none()
        if loc:
            resolved_location_id = loc.id
            resolved_warehouse = loc.display_name

    # Drives both the landing stage and the stage history written below.
    _grn_clean = (grn_number or "").strip()
    _has_grn = bool(_grn_clean)

    # qty arrives as free text; a non-numeric value must degrade to 1, not 500.
    def _safe_qty(s):
        try:
            n = int(str(s).strip())
            return n if n > 0 else 1
        except (ValueError, TypeError):
            return 1

    device = Device(
        barcode=barcode, lot_id=lot_id,
        entity=entity or None,
        sub_category=sub_category or None,
        brand=brand or None, model=model or None, device_type=device_type or None,
        serial_no=serial_no or None,
        grn_number=grn_number or None,
        cpu=cpu or None, cpu_make=cpu_make or None, generation=generation or None,
        ram_summary=ram_summary or None, hdd_summary=hdd_summary or None,
        total_ram_count=total_ram_count or None, total_ram_size=total_ram_size or None,
        total_hdd_count=total_hdd_count or None, total_hdd_size=total_hdd_size or None,
        screen_size=screen_size or None,
        battery_health_pct=int(battery_health_pct) if (battery_health_pct or "").strip().isdigit() else None,
        bios_password=(bios_password == "yes"),
        color=color or None,
        invoice_number=invoice_number or None,
        grade=grade or None,
        # A GRN Number entered here registers the tag straight into Stock
        # Inward; without one it stays at IQC and shows up on GRN in TRC's
        # pending list, to be mapped there. Same rule the mapping flow applies,
        # just applied at entry when the GRN is already known.
        current_stage=DeviceStage.stock_in if _has_grn else DeviceStage.iqc,
        floor=floor or None, warehouse=resolved_warehouse,
        location_id=resolved_location_id, notes=notes or None,
        lot_line_item_id=lot_line_item_id or None,
        qty=_safe_qty(qty),
    )

    # Auto-set device_price from LotLineItem unit_price (or lot average as fallback)
    if lot_line_item_id:
        li_result = await db.execute(
            select(LotLineItem).where(LotLineItem.id == lot_line_item_id)
        )
        line_item = li_result.scalar_one_or_none()
        if line_item and line_item.unit_price:
            device.device_price = float(line_item.unit_price)
    if not device.device_price:
        lot_result = await db.execute(select(Lot).where(Lot.id == lot_id))
        lot_obj = lot_result.scalar_one_or_none()
        if lot_obj and lot_obj.buying_price and lot_obj.qty:
            device.device_price = float(lot_obj.buying_price / lot_obj.qty)

    # Manual price override — takes priority over auto-calculated value
    if device_price_input:
        try:
            device.device_price = float(device_price_input)
        except ValueError:
            pass  # silently ignore non-numeric input

    db.add(device)
    try:
        # Flush surfaces bad enum values / constraint violations here rather
        # than as an opaque 500 at commit time.
        await db.flush()
    except Exception as exc:
        _keep_after_rollback(db, current_user)  # before rollback — see _iqc_form_boundary's wrapper for why
        await db.rollback()
        lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
        lots = lots_result.scalars().all()
        return templates.TemplateResponse("iqc/form.html", {
            "request": request, "lots": lots, "current_user": current_user,
            "error": f"Could not save this device: {str(exc)[:200]}",
        })

    await ensure_stage_location(db, device, device.current_stage, current_user,
                                preferred_location_id=device.location_id)

    # Save physical inspection data
    def _v(s): return s or None
    def _iv(s):
        try:
            return int(s) if s not in (None, "") else None
        except (ValueError, TypeError):
            return None
    inspection = IQCInspection(
        device_id=device.id,
        inspector_name=current_user.full_name,
        power_on=_v(power_on), status=_v(status), all_ok=_v(all_ok),
        bios_password=_v(bios_password) if bios_password not in ("", "yes", "no") else ("Yes" if bios_password == "yes" else None),
        r2v3_grade_category=_v(r2v3_grade_category),
        screen_dot=_v(screen_dot), screen_line=_v(screen_line),
        screen_functional=_v(screen_functional), screen_discoloration=_v(screen_discoloration),
        screen_patch=_v(screen_patch), screen_broken=_v(screen_broken),
        touch_screen=_v(touch_screen),
        screen_flickering=_v(screen_flickering), screen_scratch=_v(screen_scratch),
        screen_loose=_v(screen_loose), screen_missing=_v(screen_missing),
        screen_hinge_broken=_v(screen_hinge_broken), screen_colour_spread=_v(screen_colour_spread),
        screen_keyboard_mark=_v(screen_keyboard_mark), screen_hard_press=_v(screen_hard_press),
        panel_a_scratch=_v(panel_a_scratch), panel_a_broken=_v(panel_a_broken),
        panel_a_missing=_v(panel_a_missing), panel_a_dent=_v(panel_a_dent),
        panel_a_colour_fade=_v(panel_a_colour_fade),
        panel_b_scratch=_v(panel_b_scratch), panel_b_colour_fade=_v(panel_b_colour_fade),
        panel_b_rubber_cut=_v(panel_b_rubber_cut), panel_b_broken=_v(panel_b_broken),
        panel_b_missing=_v(panel_b_missing),
        panel_c_scratch=_v(panel_c_scratch), panel_c_broken=_v(panel_c_broken),
        panel_c_missing=_v(panel_c_missing), panel_c_dent=_v(panel_c_dent),
        panel_c_colour_fade=_v(panel_c_colour_fade),
        panel_d_dent=_v(panel_d_dent), panel_d_colour_fade=_v(panel_d_colour_fade),
        panel_d_scratch=_v(panel_d_scratch), panel_d_broken=_v(panel_d_broken),
        panel_d_missing=_v(panel_d_missing),
        keyboard_working=_v(keyboard_working), keyboard_colour_fade=_v(keyboard_colour_fade),
        keyboard_key_missing=_v(keyboard_key_missing), keyboard_hard_press=_v(keyboard_hard_press),
        speaker_status=_v(speaker_status),
        touchpad_working=_v(touchpad_working), touchpad_click_working=_v(touchpad_click_working),
        touchpad_scratch=_v(touchpad_scratch), touchpad_colour_fade=_v(touchpad_colour_fade),
        touchpad_missing=_v(touchpad_missing),
        port_hdmi=_v(port_hdmi), port_usb_working=_v(port_usb_working),
        port_audio_jack=_v(port_audio_jack),
        usb_a_ports=_iv(usb_a_ports), usb_c_ports=_iv(usb_c_ports),
        ethernet_ports=_iv(ethernet_ports),
        wifi_status=_v(wifi_status), webcam_status=_v(webcam_status),
        hdd_connector=_v(hdd_connector), hdd_casing=_v(hdd_casing),
        battery_present=_v(battery_present), battery_cable=_v(battery_cable),
        charging_port=_v(charging_port),
        dvd_drive=_v(dvd_drive),
        cover_ram=_v(cover_ram), cover_dvd=_v(cover_dvd), cover_storage=_v(cover_storage),
        hinge_condition=_v(hinge_condition), hinge_cover=_v(hinge_cover),
        touchpad_logicboard=_v(touchpad_logicboard),
        storage_health_pct=_iv(storage_health_pct), fan_sound_dba=_iv(fan_sound_dba),
        fan_working=_v(fan_working),
    )
    db.add(inspection)

    # The IQC step is always recorded — the inspection genuinely happened, and
    # dropping it would hide the tag from IQC-stage history and aging. When a
    # GRN promotes the tag immediately, that entry is closed off the same way
    # the GRN-mapping flow closes it (exited_at) and a second row carries the
    # move to Stock Inward.
    _now = app_now()
    entry = StageMovement(
        device_id=device.id, from_stage=None, to_stage=DeviceStage.iqc,
        moved_by=current_user.username, notes="IQC Entry",
        exited_at=_now if _has_grn else None,
    )
    db.add(entry)
    if _has_grn:
        db.add(StageMovement(
            device_id=device.id, from_stage=DeviceStage.iqc,
            to_stage=DeviceStage.stock_in, moved_by=current_user.username,
            notes=f"GRN {_grn_clean} entered at IQC — moved to Stock Inward",
        ))

    await audit(db, action="DEVICE_IQC_REGISTERED", user=current_user,
                table_name="devices", record_id=str(device.id),
                new_value={"barcode": barcode, "lot_id": lot_id, "brand": brand,
                           "model": model, "grade": grade, "status": status},
                request=request)

    try:
        await db.commit()
    except Exception as exc:
        # Surface constraint violations etc. as a form error instead of a 500.
        await db.rollback()
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/iqc/new?error={quote('Could not save IQC entry: ' + str(exc)[:180])}",
            status_code=302,
        )
    return RedirectResponse(url="/iqc?success=Device+added+to+IQC", status_code=302)


@router.get("/lookup", response_class=JSONResponse)
async def lookup_device(barcode: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.barcode == barcode, Device.is_active.is_(True), Device.is_trashed.is_(False))
    )
    row = result.first()
    if not row:
        return JSONResponse({"found": False})
    device, lot_number = row
    return JSONResponse({
        "found": True,
        "barcode": device.barcode,
        "brand": device.brand,
        "model": device.model,
        "device_type": device.device_type,
        "sub_category": device.sub_category,
        "serial_no": device.serial_no,
        "grn_number": device.grn_number,
        "cpu": device.cpu,
        "cpu_make": device.cpu_make,
        "generation": device.generation,
        "ram_summary": device.ram_summary,
        "hdd_summary": device.hdd_summary,
        "total_ram_count": device.total_ram_count,
        "total_ram_size": device.total_ram_size,
        "total_hdd_count": device.total_hdd_count,
        "total_hdd_size": device.total_hdd_size,
        "screen_size": device.screen_size,
        "battery_health_pct": device.battery_health_pct,
        "bios_password": device.bios_password,
        "color": device.color,
        "grade": device.grade,
        "floor": device.floor,
        "warehouse": device.warehouse,
        "current_stage": device.current_stage,
        "lot_number": lot_number,
        "lot_id": str(device.lot_id),
    })
