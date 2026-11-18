import asyncio, aiohttp, sys
sys.stdout.reconfigure(encoding="utf-8")

LOT_ID = "eec22975-1785-439c-a024-05c5b2c83812"
DEVICE_ID = "11fe6026-beb4-4ceb-b582-3e9725a127a0"
DEVICE_BARCODE = "OXY-TEST-00002"

TESTS = [
    ("HLTH-01 Health check", "GET", "http://localhost:8000/health", None, '"status"'),
    ("AUTH-01 Login page", "GET", "http://localhost:8000/auth/login", None, "Login"),
    ("DASH-01 Dashboard stats", "GET", "http://localhost:8000/", None, "IQC Pending"),
    ("DASH-02 Stage pipeline", "GET", "http://localhost:8000/", None, "Stage Pipeline"),
    ("DASH-03 Lot P&L table", "GET", "http://localhost:8000/", None, "lot-pl"),
    ("DASH-04 Location gap alert", "GET", "http://localhost:8000/", None, "Location Gaps"),
    ("DEV-01 Device list", "GET", "http://localhost:8000/devices", None, "Inventory Search"),
    ("DEV-02 Search filter", "GET", "http://localhost:8000/devices?q=OXY-TEST-00001", None, "OXY-TEST-00001"),
    ("DEV-03 Stage filter", "GET", "http://localhost:8000/devices?stage=l1", None, "L1 Repair"),
    ("DEV-04 Grade filter", "GET", "http://localhost:8000/devices?grade=A", None, "Inventory Search"),
    ("DEV-05 Device detail", "GET", f"http://localhost:8000/devices/{DEVICE_BARCODE}", None, DEVICE_BARCODE),
    ("DEV-06 Device edit form", "GET", f"http://localhost:8000/devices/{DEVICE_BARCODE}/edit", None, "Edit Device"),
    ("DEV-07 Excel export", "GET", "http://localhost:8000/devices/export", None, None),   # .xlsx (zipped) since 2026-11-18
    ("LOT-01 Lots list", "GET", "http://localhost:8000/lots", None, "Lots"),
    ("LOT-02 Lot detail", "GET", f"http://localhost:8000/lots/{LOT_ID}", None, "LOT-TEST-001"),
    ("LOT-03 New lot form", "GET", "http://localhost:8000/lots/new", None, "Lot"),
    ("LOT-04 Stock In list", "GET", "http://localhost:8000/stock", None, "Stock"),
    ("IQC-01 IQC list", "GET", "http://localhost:8000/iqc", None, "IQC"),
    ("IQC-02 IQC form", "GET", "http://localhost:8000/iqc/new", None, "IQC"),
    ("IQC-03 Lookup API", "GET", "http://localhost:8000/iqc/lookup?barcode=OXY-TEST-00001", None, None),
    ("REP-01 L1 queue", "GET", "http://localhost:8000/repair/l1", None, "L1"),
    # REP-02/REP-03 retired: the /repair/l2 and /repair/l3 pages are withdrawn and now 404.
    ("REP-04 L3/L4 queue", "GET", "http://localhost:8000/repair/l3l4", None, "L3"),
    ("REP-05 Move form", "GET", "http://localhost:8000/repair/move/form", None, "Move"),
    ("QC-01 QC list", "GET", "http://localhost:8000/qc", None, "QC"),
    ("QC-02 QC new form", "GET", "http://localhost:8000/qc/new", None, "QC"),
    ("QC-03 QC form prefilled", "GET", f"http://localhost:8000/qc/new?barcode={DEVICE_BARCODE}", None, DEVICE_BARCODE),
    ("SALE-01 Ready to sale", "GET", "http://localhost:8000/sales/ready", None, "Ready"),
    ("SALE-02 New sale form", "GET", "http://localhost:8000/sales/new", None, "Sale"),
    ("SALE-03 Sales history", "GET", "http://localhost:8000/sales", None, "Sales"),
    ("SALE-04 Returns list", "GET", "http://localhost:8000/returns", None, "Return"),
    ("SALE-05 New return form", "GET", "http://localhost:8000/returns/new", None, "Return"),
    ("PART-01 Spare parts", "GET", "http://localhost:8000/spare-parts", None, "Spare"),
    ("PART-02 New part form", "GET", "http://localhost:8000/spare-parts/new", None, "Part"),
    ("PART-03 Purchase log", "GET", "http://localhost:8000/spare-parts/purchase", None, "Purchase"),
    ("RPT-01 Lot P&L", "GET", "http://localhost:8000/reports/lot-pl", None, "Lot"),
    ("RPT-02 Stage movement", "GET", "http://localhost:8000/reports/stage-movement", None, "Stage"),
    ("RPT-03 Sales report", "GET", "http://localhost:8000/reports/sales", None, "Sales"),
    ("RPT-04 Export lot-pl CSV", "GET", "http://localhost:8000/reports/export/lot-pl", None, "LOT"),
    ("RPT-05 Export sales CSV", "GET", "http://localhost:8000/reports/export/sales", None, None),
    ("ADM-01 Users list", "GET", "http://localhost:8000/admin/users", None, "admin"),
    ("ADM-02 New user form", "GET", "http://localhost:8000/admin/users/new", None, "User"),
    ("ADM-03 Login log", "GET", "http://localhost:8000/admin/login-log", None, "Login"),
    ("ADM-04 Master data", "GET", "http://localhost:8000/admin/master", None, "Master"),
    ("ADM-05 Stage control", "GET", "http://localhost:8000/stage-control", None, "Stage"),
    ("LOC-01 Location dashboard", "GET", "http://localhost:8000/locations/dashboard", None, "Location"),
    ("LOC-02 Location master", "GET", "http://localhost:8000/locations/master", None, "Location"),
    ("LOC-03 Gap alerts", "GET", "http://localhost:8000/locations/gaps", None, "Gap"),
    ("LOC-04 Audit list", "GET", "http://localhost:8000/locations/audit", None, "Audit"),
    ("LOC-05 Device location", "GET", f"http://localhost:8000/locations/device/{DEVICE_ID}", None, "Location"),
    ("LOC-06 API device-location", "GET", f"http://localhost:8000/locations/api/device-location/{DEVICE_BARCODE}", None, None),
    ("LOC-07 API gap-count", "GET", "http://localhost:8000/locations/api/gap-count", None, None),
    ("COS-01 Cosmetic dashboard", "GET", "http://localhost:8000/cosmetic", None, "Cosmetic"),
    ("TRF-01 Transfers list", "GET", "http://localhost:8000/transfers", None, "Transfer"),
    ("TRF-02 New transfer form", "GET", "http://localhost:8000/transfers/new", None, "Transfer"),
    ("ATT-01 Attendance", "GET", "http://localhost:8000/attendance", None, "Attendance"),
    ("DLR-01 Dealers list", "GET", "http://localhost:8000/dealers", None, "Dealer"),
    ("DLR-02 Followups due", "GET", "http://localhost:8000/dealers/followups-due", None, "Follow"),
    ("GRN-01 GRN list", "GET", "http://localhost:8000/grn", None, "GRN"),
    ("GRN-02 New GRN form", "GET", "http://localhost:8000/grn/new", None, "GRN"),
    ("SC-01 Stage control", "GET", "http://localhost:8000/stage-control", None, "Stage"),
    ("SC-02 Stage aging", "GET", "http://localhost:8000/stage-control/aging", None, None),
    ("MKT-01 Market dashboard", "GET", "http://localhost:8000/market", None, "Market"),
    ("BLK-01 Bulk upload", "GET", "http://localhost:8000/bulk-upload", None, "Bulk"),
    ("TEL-01 Telecalling", "GET", "http://localhost:8000/telecalling", None, "Telecalling"),
    ("WA-01 WhatsApp", "GET", "http://localhost:8000/whatsapp", None, "WhatsApp"),
    ("GRN-03 Lot register", "GET", f"http://localhost:8000/lots/{LOT_ID}/register", None, "Register"),
]

async def run_test(session, tid, method, url, body, expected):
    try:
        resp = await session.get(url, allow_redirects=True) if method == "GET" else await session.post(url, data=body, allow_redirects=True)
        status = resp.status
        text = await resp.text()
        server_error = "Internal Server Error" in text
        content_ok = expected.lower() in text.lower() if expected else True
        passed = status == 200 and not server_error and content_ok
        note = ("Server Error" if server_error else ("Missing: " + expected if not content_ok else ""))
        return tid, passed, status, note
    except Exception as e:
        return tid, False, 0, str(e)[:80]

async def main():
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        r = await session.post("http://localhost:8000/auth/login", data={"username":"admin","password":"oxypc@admin123"}, allow_redirects=True)
        print(f"Login: HTTP {r.status} -> {r.url}")

        results = await asyncio.gather(*[run_test(session, *t) for t in TESTS])

        passed = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]

        print(f"\nFUNCTIONAL TEST RESULTS: {len(passed)} PASS / {len(failed)} FAIL / {len(results)} TOTAL\n")

        if failed:
            print("=== FAILURES ===")
            for tid, ok, status, note in failed:
                print(f"  FAIL [{status}] {tid} -- {note}")
            print()

        print("=== FULL RESULTS ===")
        for tid, ok, status, note in results:
            suffix = f" -- {note}" if note else ""
            print(f"  {'PASS' if ok else 'FAIL'} [{status}] {tid}{suffix}")

asyncio.run(main())
