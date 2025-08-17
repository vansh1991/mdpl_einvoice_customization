import frappe
from india_compliance.gst_india.utils.e_invoice import generate_e_invoice

def generate_einvoice_on_sales_return(doc, method):
    if doc.is_return and not doc.get("irn"):
        try:
            generate_e_invoice(doc.name, throw=True)
        except Exception as e:
            frappe.log_error(
                title="e-Invoice generation failed for Sales Return",
                message=frappe.get_traceback()
            )
