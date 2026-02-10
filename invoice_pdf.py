from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib import colors
from datetime import datetime


def build_invoice_pdf(
    invoice_no,
    delivery_method,
    cards_total_sgd,
    delivery_fee_sgd,
    total_sgd,
    paynow_number,
    paynow_name,
    buyer_username="",
    buyer_address="",
    items=None
):
    """
    Professional invoice with:
    - Logo
    - Item list
    - Buyer address (tracked only)
    - PayNow QR code
    - Terms & conditions
    """

    if items is None:
        items = []

    from io import BytesIO
    buffer = BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()

    elements = []

    # ===== LOGO =====
    try:
        logo = Image("logo.png", width=60*mm, height=60*mm)
        elements.append(logo)
    except:
        elements.append(Paragraph("NightShade PokeClaims", styles["Title"]))

    elements.append(Spacer(1, 10))

    # ===== HEADER =====
    elements.append(Paragraph("<b>INVOICE</b>", styles["Heading1"]))
    elements.append(Spacer(1, 10))

    # ===== SELLER INFO =====
    seller_info = f"""
    <b>Seller:</b> NightShade PokeClaims<br/>
    <b>Telegram:</b> @NightShadePokeClaims<br/>
    <b>Phone:</b> +65 93385994<br/>
    <b>Date:</b> {datetime.now().strftime("%d %b %Y")}<br/>
    <b>Invoice #:</b> {invoice_no}
    """

    elements.append(Paragraph(seller_info, styles["Normal"]))
    elements.append(Spacer(1, 10))

    # ===== BUYER INFO =====
    if delivery_method == "tracked":
        address_display = buyer_address or "Pending"
    else:
        address_display = "N/A (Self Collection)"

    buyer_info = f"""
    <b>Buyer Telegram Username:</b> @{buyer_username or "N/A"}<br/>
    <b>Delivery Method:</b> {delivery_method.upper()}<br/>
    <b>Shipping Address:</b><br/>{address_display}
    """

    elements.append(Paragraph(buyer_info, styles["Normal"]))
    elements.append(Spacer(1, 10))

    # ===== ITEM TABLE =====
    table_data = [["Item Description", "Qty", "Unit Price (SGD)", "Line Total (SGD)"]]

    for item in items:
        name = item.get("name")
        qty = int(item.get("qty", 1))
        raw_price = item.get("price", 0)

        if isinstance(raw_price, str):
            raw_price = (
                raw_price
                .replace("SGD", "")
                .replace("$", "")
                .replace(",", "")
                .strip()
            )
    
        price = float(raw_price)
    
        table_data.append([
            name,
            str(qty),
            f"${price:.2f}",
            f"${price * qty:.2f}"
        ])


    table = Table(table_data, colWidths=[80*mm, 20*mm, 35*mm, 35*mm])

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")
    ]))

    elements.append(table)
    elements.append(Spacer(1, 10))

    # ===== TOTALS =====
    totals_table = Table([
        ["Cards Total:", f"SGD {cards_total_sgd:.2f}"],
        ["Delivery Fee:", f"SGD {delivery_fee_sgd:.2f}"],
        ["TOTAL DUE:", f"SGD {total_sgd:.2f}"],
    ], colWidths=[120*mm, 50*mm])

    totals_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
    ]))

    elements.append(totals_table)
    elements.append(Spacer(1, 10))

    # ===== PAYMENT INSTRUCTIONS =====
    payment_text = f"""
    <b>Payment Instructions</b><br/>
    - PayNow to: {paynow_number} ({paynow_name})<br/>
    - Send payment screenshot via Telegram<br/>
    - Include invoice number in payment notes<br/>
    """

    elements.append(Paragraph(payment_text, styles["Normal"]))
    elements.append(Spacer(1, 10))

    # ===== PAYNOW QR CODE =====
    try:
        qr = Image("paynow_qr.png", width=50*mm, height=50*mm)
        elements.append(qr)
        elements.append(Spacer(1, 10))
    except:
        elements.append(Paragraph("PayNow QR Code: (image not found)", styles["Normal"]))

    # ===== TERMS & CONDITIONS =====
    terms = """
    <b>Terms & Conditions</b><br/>
    - All sales are final after payment is made.<br/>
    - Please verify card condition upon receiving.<br/>
    - Tracking number will be provided once shipped.<br/>
    - Seller is not responsible for courier delays.<br/>
    - Self-collection orders must be collected within 14 days.<br/>
    - Uncollected orders may be cancelled after grace period.<br/>
    """

    elements.append(Paragraph(terms, styles["Normal"]))
    elements.append(Spacer(1, 20))

    elements.append(Paragraph(
        "Thank you for your purchase! – NightShade PokeClaims",
        styles["Italic"]
    ))

    doc.build(elements)

    pdf = buffer.getvalue()
    buffer.close()

    return pdf
