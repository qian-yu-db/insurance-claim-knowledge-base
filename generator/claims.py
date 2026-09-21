"""Shared, internally-consistent entity definitions for 3 synthetic auto-collision claims.

Every generated artifact (PDF, .eml, image, docx, audio, long-tail) pulls its
identifiers from here, so a claim reads as one coherent bundle: the same claim
number, policy number, insured, loss date, parties, and vehicle thread through
every file. This is the single source of truth for the ground-truth manifest too.
"""

from pathlib import Path

# ---- paths ----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DATA = PROJECT_ROOT / "sample_data"
CLAIMS_ROOT = SAMPLE_DATA / "claims"
# Source assets we reuse/adapt:
DAMAGE_IMG_DIR = Path.home() / "Downloads" / "dbdemos-dataset-smartclaims" / "fsi" / "smart-claims" / "Images"
EXISTING_MP3_DIR = SAMPLE_DATA  # the 5 policy_no_*.mp3 files live at sample_data root

# ---- the three claims -----------------------------------------------------
# Severity buckets in the damage image set: ok / minor / major.
CLAIMS = {
    # ------------------------------------------------------------------ A
    "CLM-2026-04412": {
        "severity": "minor",
        "complexity": "fast-track",
        "carrier": "Summit Ridge Casualty",
        "policy_no": "PA-2026-101618576",
        "policy_form": "Personal Auto Policy (PAP)",
        "policy_period": ("2025-11-01", "2026-11-01"),
        "reuse_mp3": "policy_no_101618576.mp3",
        "insured": {
            "name": "Marcus T. Okonkwo",
            "address": "2217 Sycamore Court, Lakeside, OH 44311",
            "phone": "(419) 555-0148",
            "email": "mokonkwo@example.com",
        },
        "vehicle": {"year": 2022, "make": "Honda", "model": "Accord EX", "vin": "1HGCV1F13NA004217", "plate": "OH-JHK-2291"},
        "loss": {
            "date": "2026-03-04",
            "reported": "2026-03-05",
            "location": "Intersection of Maple Ave & 4th St, Lakeside, OH",
            "peril": "Vehicle collision",
            "description": "Insured was rear-ended at a stop light. Low-speed impact, rear bumper and trunk lid damage. No injuries reported.",
            "liability": "Clear — third party at fault (rear-end).",
        },
        "adjuster": {"name": "Terrence W. Buell", "license": "OH-CLM-72014", "phone": "(419) 555-0191", "email": "tbuell@summitridge.example.com"},
        "third_party": {"name": "Gregory P. Salas", "insurer": "Harbor Mutual Insurance", "claim_ref": "HM-CLM-88231"},
        "estimate": {"parts": 1840.00, "labor": 720.00, "paint": 410.00, "tax": 190.25, "total": 3160.25, "deductible": 500.00, "net": 2660.25},
        "settlement": {"amount": 2660.25, "date": "2026-03-19", "status": "Paid"},
    },
    # ------------------------------------------------------------------ B
    "CLM-2026-04487": {
        "severity": "major",
        "complexity": "litigation-bodily-injury",
        "carrier": "Harbor Mutual Insurance",
        "policy_no": "PA-2026-101618579",
        "policy_form": "Personal Auto Policy (PAP)",
        "policy_period": ("2025-08-15", "2026-08-15"),
        "reuse_mp3": "policy_no_101618579.mp3",
        "insured": {
            "name": "Patricia J. Carmichael",
            "address": "48 Ridgewood Lane, Fairview, TX 75069",
            "phone": "(972) 555-0184",
            "email": "pcarmichael@example.com",
        },
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Highlander XLE", "vin": "5TDGZRBH8PS123884", "plate": "TX-RLM-8842"},
        "loss": {
            "date": "2026-02-11",
            "reported": "2026-02-11",
            "location": "US-75 Frontage Rd & Parker Rd, Plano, TX",
            "peril": "Vehicle collision",
            "description": "Insured struck claimant vehicle while merging; high-speed impact. Claimant (Denise R. Varga) transported by ambulance, cervical and lumbar injuries alleged. Total loss to claimant vehicle. Litigation filed.",
            "liability": "Contested — comparative negligence alleged; insured presumed majority at fault.",
        },
        "adjuster": {"name": "Renata C. Diaz", "license": "TX-CLM-55130", "phone": "(972) 555-0202", "email": "rdiaz@harbormutual.example.com"},
        "claimant": {
            "name": "Denise R. Varga",
            "address": "904 Coral Drive, Marigold Beach, FL 34231",
            "phone": "(941) 555-0177",
            "attorney": {"name": "Elena Ramirez, Esq.", "firm": "Ramirez Law Group", "email": "eramirez@ramirezlaw.example.com", "phone": "(214) 555-0330"},
        },
        "defense_counsel": {"name": "Harold V. Nguyen, Esq.", "firm": "Nguyen & Associates", "email": "hnguyen@nguyenlaw.example.com", "phone": "(469) 555-0451"},
        "medical": {
            "provider": "Lakeside Orthopedic & Spine Center",
            "physician": "Dr. Anita S. Reyes, MD",
            "records_pages": 108,  # the 100+ page bundle target
            "diagnoses": ["Cervical strain (S13.4XXA)", "Lumbar disc herniation L4-L5 (M51.26)", "Post-concussive syndrome (F07.81)"],
            "billed": 74210.00,
        },
        "reserves": {"bi": 150000.00, "pd": 38500.00},
        "demand": {"amount": 225000.00, "date": "2026-06-02", "by": "Ramirez Law Group"},
        "settlement": {"amount": 132500.00, "date": "2026-08-27", "status": "Negotiated — pending release"},
    },
    # ------------------------------------------------------------------ C
    "CLM-2026-04531": {
        "severity": "moderate",
        "complexity": "investigation-disputed",
        "carrier": "Blue Meridian Insurance",
        "policy_no": "PA-2026-101618580",
        "policy_form": "Personal Auto Policy (PAP)",
        "policy_period": ("2026-01-10", "2027-01-10"),
        "reuse_mp3": "policy_no_101618580.mp3",
        "insured": {
            "name": "Angela M. Foster",
            "address": "715 Birchwood Terrace, Grover, NC 28073",
            "phone": "(704) 555-0119",
            "email": "afoster@example.com",
        },
        "vehicle": {"year": 2021, "make": "Subaru", "model": "Outback Premium", "vin": "4S4BTAFC5M3201765", "plate": "NC-HGT-4410"},
        "loss": {
            "date": "2026-04-19",
            "reported": "2026-04-22",
            "location": "Rural Rte 9, near Grover, NC (no witnesses)",
            "peril": "Vehicle collision",
            "description": "Single-vehicle collision reported 3 days late. Insured claims a deer strike; damage pattern and SIU review flag possible inconsistency with reported cause. Referred to Special Investigations Unit.",
            "liability": "Under investigation — cause of loss disputed.",
        },
        "adjuster": {"name": "Marcus D. Hale", "license": "NC-CLM-41902", "phone": "(704) 555-0155", "email": "mhale@bluemeridian.example.com"},
        "siu": {"investigator": "David K. Holt", "unit": "Special Investigations Unit", "email": "dholt@bluemeridian.example.com", "ref": "SIU-2026-0771"},
        "estimate": {"parts": 4120.00, "labor": 1960.00, "paint": 880.00, "tax": 421.00, "total": 7381.00, "deductible": 1000.00, "net": 6381.00},
        "settlement": {"amount": 0.0, "date": None, "status": "Pending investigation outcome"},
    },
}

# Which damage images (by severity prefix) each claim draws from, and how many.
CLAIM_IMAGE_PLAN = {
    "CLM-2026-04412": {"minor": 2, "ok": 1},
    "CLM-2026-04487": {"major": 4},
    "CLM-2026-04531": {"minor": 2, "ok": 2},
}
