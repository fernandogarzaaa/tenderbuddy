"""Tenderbuddy product seed: company profile, 12 tenders, source records.

Idempotent: rows are keyed on natural keys (profile id=1, tender ref_no,
source name) and skipped when they already exist. Deadlines are seeded
relative to today so dashboard alerts always have something to show.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from app import models
from app.models import naive_utcnow

SOURCES = ["GeM", "CPP Portal", "eProcure"]


def _tenders() -> list[dict]:
    now = naive_utcnow()

    def t(days_pub: int, days_due: int, **kw) -> dict:
        kw["published_date"] = now - timedelta(days=days_pub)
        kw["deadline"] = now + timedelta(days=days_due)
        return kw

    return [
        t(
            6, 5,
            title="Supply of Desktop Computers and All-in-One PCs",
            ref_no="GEM/2026/B/6123451",
            authority="Ministry of Electronics and IT",
            category="IT Services",
            state="Karnataka",
            est_value_lakh=120.0,
            emd_lakh=2.4,
            turnover_req_cr=8.0,
            exp_req_years=3,
            certs_req=["ISO 9001:2015", "GeM Registered"],
            description="Supply, installation and 3-year onsite warranty of 150 desktop computers and 40 all-in-one PCs for regional offices. Bidders must have OEM authorization.",
            source="GeM",
        ),
        t(
            10, 3,
            title="Annual Maintenance Contract for CCTV Surveillance System",
            ref_no="CPP/2026/AMC/08821",
            authority="Delhi Police Headquarters",
            category="IT Services",
            state="Delhi",
            est_value_lakh=45.0,
            emd_lakh=0.9,
            turnover_req_cr=2.0,
            exp_req_years=2,
            certs_req=["MSME Registered"],
            description="Comprehensive AMC for 320 CCTV cameras, NVRs and video wall across 4 buildings for 12 months. Includes quarterly preventive maintenance and 24x7 breakdown support with 4-hour SLA.",
            source="CPP Portal",
        ),
        t(
            12, 9,
            title="Construction of Additional Classroom Block at Government School",
            ref_no="CPWD/EE-D/2026/114",
            authority="Central Public Works Department",
            category="Works",
            state="Maharashtra",
            est_value_lakh=380.0,
            emd_lakh=7.6,
            turnover_req_cr=30.0,
            exp_req_years=7,
            certs_req=["ISO 9001:2015", "Contractor Class A"],
            description="G+2 RCC classroom block (12 classrooms, toilets, staircase) including electrical, plumbing and finishing works. Completion period 12 months. Liquidated damages 0.5% per week of delay.",
            source="eProcure",
        ),
        t(
            4, 12,
            title="Supply of Office Stationery on Rate Contract",
            ref_no="GEM/2026/B/6127780",
            authority="Department of Administrative Reforms",
            category="Supply",
            state="Delhi",
            est_value_lakh=28.0,
            emd_lakh=0.0,
            turnover_req_cr=1.0,
            exp_req_years=1,
            certs_req=["GeM Registered"],
            description="Two-year rate contract for supply of office stationery items (210 line items) to 14 offices in Delhi NCR. EMD exempted for MSE bidders. Quarterly billing.",
            source="GeM",
        ),
        t(
            8, 6,
            title="Catering Services for Railway Station Food Plaza",
            ref_no="RLY/CATG/2026/045",
            authority="IRCTC / Northern Railway",
            category="Supply",
            state="Delhi",
            est_value_lakh=210.0,
            emd_lakh=4.2,
            turnover_req_cr=15.0,
            exp_req_years=5,
            certs_req=["FSSAI License", "ISO 22000"],
            description="License for operating food plaza at New Delhi Railway Station for 5 years. Minimum annual license fee Rs 42 lakh. Performance security of 10% required via bank guarantee.",
            source="CPP Portal",
        ),
        t(
            15, 15,
            title="Development of Citizen Grievance Redressal Portal",
            ref_no="GEM/2026/B/6119002",
            authority="Karnataka State e-Governance Mission",
            category="IT Services",
            state="Karnataka",
            est_value_lakh=95.0,
            emd_lakh=1.9,
            turnover_req_cr=5.0,
            exp_req_years=4,
            certs_req=["ISO 9001:2015", "ISO 27001", "GeM Registered"],
            description="Design, development and 2-year maintenance of a multilingual grievance redressal portal with workflow engine, SMS/email alerts and analytics dashboard. Agile delivery in 6 sprints.",
            source="GeM",
        ),
        t(
            5, 4,
            title="Supply and Installation of 50 kW Rooftop Solar Plant",
            ref_no="MSEDCL/SOLAR/2026/311",
            authority="Maharashtra State Electricity Distribution Co.",
            category="Works",
            state="Maharashtra",
            est_value_lakh=34.0,
            emd_lakh=0.68,
            turnover_req_cr=2.5,
            exp_req_years=3,
            certs_req=["MNRE Channel Partner", "ISO 9001:2015"],
            description="Design, supply, installation and 5-year O&M of 50 kW grid-connected rooftop solar PV plant at divisional office. Net metering liaison included. Penalty for generation shortfall applies.",
            source="eProcure",
        ),
        t(
            3, 20,
            title="Hiring of Vehicles on Monthly Basis",
            ref_no="GEM/2026/B/6130204",
            authority="Income Tax Department",
            category="Supply",
            state="Delhi",
            est_value_lakh=60.0,
            emd_lakh=1.2,
            turnover_req_cr=3.0,
            exp_req_years=2,
            certs_req=["GeM Registered"],
            description="Hiring of 12 diesel SUVs with drivers on monthly basis for 24 months. Vehicles not older than 2023 model. Fuel and maintenance by bidder. Corrigendum 1 issued extending bid due date.",
            source="GeM",
        ),
        t(
            9, 2,
            title="Network LAN Upgrade for District Collectorate",
            ref_no="CPP/2026/NET/09117",
            authority="District Collectorate, Bengaluru Urban",
            category="IT Services",
            state="Karnataka",
            est_value_lakh=52.0,
            emd_lakh=1.04,
            turnover_req_cr=4.0,
            exp_req_years=3,
            certs_req=["ISO 9001:2015", "OEM Authorization"],
            description="Supply and laying of Cat6A structured cabling (400 nodes), 12 managed switches and firewall for collectorate campus. Completion within 30 days of work order. Short bidding window.",
            source="CPP Portal",
        ),
        t(
            7, 8,
            title="Printing of Voter Awareness Material",
            ref_no="CEO-MH/PRINT/2026/77",
            authority="Chief Electoral Officer, Maharashtra",
            category="Supply",
            state="Maharashtra",
            est_value_lakh=18.0,
            emd_lakh=0.36,
            turnover_req_cr=1.0,
            exp_req_years=2,
            certs_req=["MSME Registered"],
            description="Printing and district-wise distribution of 5 lakh pamphlets, 20,000 posters and 500 hoardings for voter awareness campaign. Delivery within 21 days.",
            source="eProcure",
        ),
        t(
            11, 25,
            title="Consultancy for Smart City Traffic Survey",
            ref_no="GEM/2026/C/6098812",
            authority="Pune Smart City Development Corp.",
            category="Consultancy",
            state="Maharashtra",
            est_value_lakh=75.0,
            emd_lakh=1.5,
            turnover_req_cr=6.0,
            exp_req_years=5,
            certs_req=["ISO 9001:2015"],
            description="Traffic volume counts, OD survey and signal timing study at 40 junctions. Deliverables include microsimulation model and DPR chapter. Key experts: transport planner, data analyst.",
            source="GeM",
        ),
        t(
            2, 11,
            title="Supply of LED Street Lights with 5-year Warranty",
            ref_no="CPP/2026/LED/09230",
            authority="Bengaluru Municipal Corporation",
            category="Supply",
            state="Karnataka",
            est_value_lakh=140.0,
            emd_lakh=2.8,
            turnover_req_cr=12.0,
            exp_req_years=4,
            certs_req=["BIS Certification", "ISO 9001:2015"],
            description="Supply of 4,000 LED street lights (120W) with 5-year replacement warranty and centralized monitoring system. Sample testing at NABL lab before bulk supply.",
            source="CPP Portal",
        ),
    ]


def seed_product(db: Session) -> dict:
    counts = {"tenders": 0, "requirements": 0, "notes": 0, "sources": 0}

    profile = db.get(models.CompanyProfile, 1)
    if not profile:
        db.add(
            models.CompanyProfile(
                id=1,
                name="Vertex Systems Pvt. Ltd.",
                turnover_cr=25.0,
                years_exp=8,
                certifications_json=json.dumps(
                    ["ISO 9001:2015", "MSME Registered", "GeM Registered", "NSIC Registered"]
                ),
                categories_json=json.dumps(["IT Services", "Supply", "Works"]),
                states_json=json.dumps(["Karnataka", "Maharashtra", "Delhi"]),
            )
        )

    for name in SOURCES:
        if not db.query(models.SourceRecord).filter_by(name=name).first():
            db.add(models.SourceRecord(name=name, last_sync_at=None))
            counts["sources"] += 1

    for row in _tenders():
        if db.query(models.Tender).filter_by(ref_no=row["ref_no"]).first():
            continue
        tender = models.Tender(
            title=row["title"],
            ref_no=row["ref_no"],
            authority=row["authority"],
            category=row["category"],
            state=row["state"],
            est_value_lakh=row["est_value_lakh"],
            emd_lakh=row["emd_lakh"],
            published_date=row["published_date"],
            deadline=row["deadline"],
            turnover_req_cr=row["turnover_req_cr"],
            exp_req_years=row["exp_req_years"],
            certs_req_json=json.dumps(row["certs_req"]),
            description=row["description"],
            source=row["source"],
            status="New",
        )
        db.add(tender)
        db.flush()  # assign id for requirement FKs
        counts["tenders"] += 1

        # Seed the document checklist: EMD proof + one item per required cert.
        docs = []
        if tender.emd_lakh and tender.emd_lakh > 0:
            docs.append(f"EMD of Rs {tender.emd_lakh} lakh (demand draft / bank guarantee)")
        else:
            docs.append("EMD exemption claim (MSE certificate)")
        docs += [f"{c} copy" for c in row["certs_req"]]
        docs += ["Company PAN and GST registration copies", "Signed tender acceptance letter"]
        for text in docs:
            db.add(
                models.Requirement(
                    tender_id=tender.id, text=text, kind="document",
                    is_met=False, auto=True,
                )
            )
            counts["requirements"] += 1

    # One sample bid note on the first tender so the notes UI is not empty.
    first = db.query(models.Tender).filter_by(ref_no="GEM/2026/B/6123451").first()
    if first and not db.query(models.BidNote).filter_by(tender_id=first.id).first():
        db.add(
            models.BidNote(
                tender_id=first.id,
                text="Good fit: we already supply to two central ministries. Check OEM authorization letter expiry before pricing.",
            )
        )
        counts["notes"] += 1

    return counts
