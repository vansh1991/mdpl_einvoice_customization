import frappe
from india_compliance.gst_india.utils.e_invoice import generate_e_invoice
from india_compliance.exceptions import NotApplicableError

def before_submit_return_invoice(doc, method):
    """Temporarily set nil_exempt treatment to Generate so IC skips the throw"""
    if doc.is_return and not doc.get("irn"):
        from india_compliance.gst_india.constants import TAXABLE_GST_TREATMENTS
        has_taxable = any(item.gst_treatment in TAXABLE_GST_TREATMENTS for item in doc.items)
        if not has_taxable:
            frappe.db.set_value("GST Settings", "GST Settings",
                "nil_exempt_e_invoice_treatment", "Generate")
            frappe.db.commit()

def generate_einvoice_on_sales_return(doc, method):
    """Restore nil_exempt treatment after submit"""
    try:
        if doc.is_return and not doc.get("irn"):
            try:
                generate_e_invoice(doc.name, throw=False)
            except NotApplicableError:
                pass
            except Exception:
                frappe.log_error(
                    title="e-Invoice generation failed for Sales Return",
                    message=frappe.get_traceback()
                )
    finally:
        # Always restore the setting regardless of success or failure
        current = frappe.db.get_single_value("GST Settings", "nil_exempt_e_invoice_treatment")
        if current == "Generate":
            frappe.db.set_value("GST Settings", "GST Settings",
                "nil_exempt_e_invoice_treatment", "Do Not Generate")
            frappe.db.commit()
