# Invoice PDF generator for "Nickyboys' Restaurant Goodies"
# - Creates a fixed product catalog with consistent pricing across all invoices
# - Generates realistic (but fictional) restaurant recipients and addresses
# - Produces a requested number of invoices as individual PDFs
# - Also writes a catalog CSV and a manifest CSV, and a ZIP of all PDFs
#
# If you want to change the number of invoices, edit OUTPUT_COUNT below.

import os
import random
import math
from datetime import datetime, timedelta
from pathlib import Path
import csv
import zipfile

# Try to import reportlab (commonly available). If it's not available,
# raise a clear error so the user knows to install it.
try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
except Exception as e:
    raise RuntimeError("This environment needs the 'reportlab' package to generate PDFs. "
                       "Please install it (e.g., `pip install reportlab`) and rerun. "
                       f"Import error: {e}")

# ----------------------- CONFIG -----------------------
OUTPUT_COUNT = 20  # Number of invoices to generate

# Put all outputs alongside this script (project folder)
BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR
CATALOG_CSV = BASE_DIR / "product_catalog.csv"
MANIFEST_CSV = BASE_DIR / "invoice_manifest.csv"
ZIP_PATH = BASE_DIR / "invoices.zip"


FROM_NAME = "Nickyboys' Restaurant Goodies"
FROM_ADDR_1 = "1000 Continental Dr."
FROM_ADDR_2 = "King of Prussia, PA 19406"
FROM_PHONE = "(610) 555-0199"
FROM_EMAIL = "orders@nicykboysrestaurantgoods.com"

# Fixed catalog: item -> (unit, price)
# You can extend freely; prices will stay consistent across all invoices.
PRODUCT_CATALOG = {
    "Stainless Steel Chef Knife 8in": ("each", 34.99),
    "Paring Knife 3.5in": ("each", 12.50),
    "Silver Fork": ("each", 5.00),
    "Dinner Spoon": ("each", 4.50),
    "Soup Ladle": ("each", 11.25),
    "Wooden Spatula": ("each", 3.99),
    "Silicone Spatula": ("each", 5.49),
    "Nonstick Fry Pan 10in": ("each", 39.95),
    "Cast Iron Skillet 12in": ("each", 54.00),
    "Stainless Sauce Pan 2qt": ("each", 29.99),
    "Stainless Stock Pot 12qt": ("each", 79.00),
    "Cutting Board - Maple": ("each", 22.75),
    "Cutting Board - Plastic": ("each", 12.99),
    "Mixing Bowl 3qt": ("each", 8.25),
    "Mixing Bowl 5qt": ("each", 11.50),
    "Sheet Pan Half": ("each", 9.25),
    "Sheet Pan Full": ("each", 14.75),
    "Baking Parchment Roll": ("roll", 7.80),
    "Squeeze Bottle 16oz": ("each", 1.99),
    "Food Storage Container 2qt": ("each", 6.25),
    "Food Storage Container 4qt": ("each", 8.10),
    "Cambro Lid (fits 2–4qt)": ("each", 2.10),
    "Disposable Gloves (Box 100)": ("box", 7.99),
    "Chef Apron": ("each", 12.00),
    "Digital Thermometer": ("each", 18.95),
    "Kitchen Towels (Pack of 6)": ("pack", 10.99),
    "Aluminum Foil Heavy Duty 1000ft": ("roll", 29.50),
    "Plastic Wrap 2000ft": ("roll", 24.25),
    "Bus Tub": ("each", 9.99),
    "Dish Rack": ("each", 16.50),
    "Serrated Bread Knife 10in": ("each", 24.75),
    "Tongs 12in": ("each", 4.60),
    "Whisk 10in": ("each", 3.80),
    "Mandoline Slicer": ("each", 42.00),
    "Colander Stainless 5qt": ("each", 14.20),
    "Immersion Blender": ("each", 89.00),
    "Heat-Resistant Spatula": ("each", 6.40),
    "Silicone Baking Mat Half": ("each", 11.20),
    "Grill Brush": ("each", 7.30),
    "Commercial Can Opener": ("each", 64.99),
}

# Some realistic US cities & states for restaurant recipients
CITIES = [
    ("Philadelphia", "PA", "191"),
    ("Pittsburgh", "PA", "152"),
    ("Harrisburg", "PA", "171"),
    ("Newark", "DE", "197"),
    ("Wilmington", "DE", "198"),
    ("Baltimore", "MD", "212"),
    ("Towson", "MD", "212"),
    ("Cherry Hill", "NJ", "080"),
    ("Trenton", "NJ", "086"),
    ("Camden", "NJ", "081"),
    ("New York", "NY", "100"),
    ("Yonkers", "NY", "107"),
    ("Stamford", "CT", "069"),
    ("Allentown", "PA", "181"),
    ("Reading", "PA", "196"),
    ("Lancaster", "PA", "176"),
    ("Scranton", "PA", "185"),
    ("Bethlehem", "PA", "180"),
]

STREET_NAMES = [
    "Market St", "Broad St", "Main St", "Chestnut St", "Walnut St",
    "Spruce St", "Pine St", "Maple Ave", "Elm Ave", "Second St",
    "Third St", "Fourth St", "Fifth Ave", "Park Ave", "Ridge Ave",
    "Front St", "River Rd", "High St", "Bridge St", "Union Ave"
]

RESTAURANT_PREFIX = [
    "Golden", "Red Oak", "Blue Harbor", "Sunrise", "Cedar Grove", "Harvest",
    "Liberty", "Hudson", "Riverstone", "Silver Spoon", "Firefly", "Willow",
    "Urban", "Green Leaf", "Copper Pot", "Iron Gate", "Saffron", "Juniper",
    "Nick's", "Bella", "Little"
]

RESTAURANT_SUFFIX = [
    "Bistro", "Tavern", "Kitchen", "Trattoria", "Grill", "Diner",
    "Cantina", "Ramen House", "Pizzeria", "Steakhouse", "Cafe",
    "Tap House", "Bar & Grill", "Smokehouse", "Gastropub"
]

def make_restaurant_name():
    # 50/50 chance to use a composed name vs "The X Y"
    if random.random() < 0.5:
        return f"{random.choice(RESTAURANT_PREFIX)} {random.choice(RESTAURANT_SUFFIX)}"
    else:
        noun = random.choice(["Fork", "Spatula", "Spice", "Anchor", "Lantern", "Hearth", "Crown", "Sailor", "Spoon"])
        return f"The {random.choice(['Rusty','Golden','Blue','Green','Silver','Old','Velvet'])} {noun}"

def make_address():
    number = random.randint(100, 9999)
    street = random.choice(STREET_NAMES)
    city, state, zip_prefix = random.choice(CITIES)
    zip_suffix = random.randint(10, 99)
    zipcode = f"{zip_prefix}{zip_suffix:02d}"
    return f"{number} {street}", f"{city}, {state} {zipcode}"

def random_invoice_number(i):
    return f"INV-{datetime.now().strftime('%Y%m%d')}-{i:03d}"

def choose_line_items():
    # Choose 5–12 distinct products per invoice
    n = random.randint(5, 12)
    items = random.sample(list(PRODUCT_CATALOG.items()), n)
    lines = []
    for (name, (unit, price)) in items:
        qty = random.randint(1, 40)
        lines.append((name, unit, qty, price, qty * price))
    return lines

def currency(x):
    return f"${x:,.2f}"

def draw_invoice(pdf_path, invoice_number, bill_to_name, bill_to_addr1, bill_to_addr2, issued_date, due_date, lines, tax_rate):
    doc = SimpleDocTemplate(str(pdf_path), pagesize=LETTER, leftMargin=0.7*inch, rightMargin=0.7*inch, topMargin=0.7*inch, bottomMargin=0.7*inch)
    styles = getSampleStyleSheet()
    title_style = styles["Heading1"]
    title_style.textColor = colors.HexColor("#0e7cc8")
    title_style.spaceAfter = 12

    normal = styles["Normal"]
    small = ParagraphStyle('small', parent=normal, fontSize=9)
    right = ParagraphStyle('right', parent=normal, alignment=TA_RIGHT)
    right_small = ParagraphStyle('rightsmall', parent=small, alignment=TA_RIGHT)

    story = []

    # Header
    story.append(Paragraph("<b>INVOICE</b>", title_style))

    meta = Table([
        ["Invoice No.", invoice_number],
        ["Invoice Date", issued_date.strftime("%b %d, %Y")],
        ["Due Date", due_date.strftime("%b %d, %Y")]
    ], colWidths=[1.6*inch, 2.2*inch])
    meta.setStyle(TableStyle([
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
    ]))
    story.append(meta)
    story.append(Spacer(1, 8))

    # From/To blocks
    from_block = [
        [Paragraph("<b>FROM</b>", small)],
        [Paragraph(FROM_NAME, normal)],
        [Paragraph(FROM_ADDR_1, small)],
        [Paragraph(FROM_ADDR_2, small)],
        [Paragraph(FROM_PHONE, small)],
        [Paragraph(FROM_EMAIL, small)],
    ]
    to_block = [
        [Paragraph("<b>TO</b>", small)],
        [Paragraph(bill_to_name, normal)],
        [Paragraph(bill_to_addr1, small)],
        [Paragraph(bill_to_addr2, small)],
    ]

    duo = Table([
        [Table(from_block, hAlign="LEFT"), Table(to_block, hAlign="LEFT")]
    ], colWidths=[3.4*inch, 3.4*inch])
    story.append(duo)
    story.append(Spacer(1, 12))

    # Line items table
    data = [["DESCRIPTION", "UNIT", "QTY", "RATE", "AMOUNT"]]
    for desc, unit, qty, rate, amount in lines:
        data.append([desc, unit, qty, currency(rate), currency(amount)])

    tbl = Table(data, colWidths=[3.2*inch, 0.8*inch, 0.7*inch, 1.0*inch, 1.1*inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0e7cc8")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("ALIGN", (2,1), (2,-1), "RIGHT"),
        ("ALIGN", (3,1), (4,-1), "RIGHT"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#d9e6f2")),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),
        ("FONTSIZE", (0,1), (-1,-1), 9),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f7fbff")]),
    ]))
    story.append(tbl)

    # Totals
    subtotal = sum(a for _,_,_,_,a in lines)
    tax = subtotal * tax_rate
    grand = subtotal + tax

    story.append(Spacer(1, 12))

    # Totals UNDER the items (left-aligned, fits page width)
    totals = Table(
        [
            ["Subtotal", currency(subtotal)],
            [f"Tax ({int(tax_rate * 100)}%)", currency(tax)],
            ["GRAND TOTAL", currency(grand)],
        ],
        colWidths=[2.0 * inch, 1.3 * inch],  # narrow, safe
        hAlign="LEFT"  # place directly underneath
    )
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.black),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story.append(totals)
    story.append(Spacer(1, 18))

    story.append(Spacer(1, 10))
    #story.append(Table([[Paragraph("", normal), totals]], colWidths=[5.4*inch, 1.9*inch]))
    story.append(Spacer(1, 18))
    story.append(Paragraph("<b>Thank you for your partnership!</b>", normal))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Please remit to the email or phone above. Prices are in USD.", small))

    doc.build(story)

# ----------------------- MAIN -----------------------
random.seed(42)

# Save the product catalog (for transparency/reuse—ensures stable pricing)
with open(CATALOG_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Product", "Unit", "PriceUSD"])
    for name, (unit, price) in PRODUCT_CATALOG.items():
        w.writerow([name, unit, f"{price:.2f}"])

# Generate invoices
manifest_rows = []
base_issue = datetime.now() - timedelta(days=30)
for i in range(1, OUTPUT_COUNT + 1):
    invoice_no = random_invoice_number(i)
    # Restaurant recipient
    to_name = make_restaurant_name()
    addr1, addr2 = make_address()

    # Date logic
    issued = base_issue + timedelta(days=random.randint(0, 25))
    due = issued + timedelta(days=random.choice([7, 14, 21, 30]))

    # Line items (tax varies slightly by "jurisdiction", 6%–8.5%)
    lines = choose_line_items()
    tax_rate = random.choice([0.06, 0.0625, 0.07, 0.0725, 0.08, 0.0825, 0.085])

    pdf_path = PDF_DIR / f"{invoice_no}.pdf"
    draw_invoice(pdf_path, invoice_no, to_name, addr1, addr2, issued, due, lines, tax_rate)

    subtotal = sum(a for *_, a in lines)
    tax = subtotal * tax_rate
    grand = subtotal + tax

    manifest_rows.append([
        invoice_no, issued.strftime("%Y-%m-%d"), due.strftime("%Y-%m-%d"),
        to_name, addr1, addr2,
        f"{subtotal:.2f}", f"{tax:.2f}", f"{grand:.2f}"
    ])

# Write manifest
with open(MANIFEST_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["InvoiceNo", "IssuedDate", "DueDate", "ToName", "ToAddr1", "ToAddr2", "Subtotal", "Tax", "GrandTotal"])
    w.writerows(manifest_rows)

# Zip PDFs for easy download
with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as z:
    for p in PDF_DIR.glob("*.pdf"):
        z.write(p, arcname=p.name)

# BASE_DIR, PDF_DIR, CATALOG_CSV, MANIFEST_CSV, ZIP_PATH
