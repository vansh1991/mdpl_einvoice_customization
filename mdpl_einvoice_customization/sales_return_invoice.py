import frappe
from india_compliance.gst_india.utils.e_invoice import generate_e_invoice
from india_compliance.exceptions import NotApplicableError
from india_compliance.gst_india.constants import TAXABLE_GST_TREATMENTS


def before_submit_return_invoice(doc, method):
    """
    Hook: before_submit on Sales Invoice.
    For nil/exempt sales returns without an IRN, flag the doc in memory
    so on_submit knows to attempt e-Invoice generation.
    No DB writes here — avoids race conditions on GST Settings.
    """
    if not _is_eligible_return(doc):
        return

    has_taxable = any(
        item.gst_treatment in TAXABLE_GST_TREATMENTS
        for item in doc.items
    )

    if not has_taxable:
        # In-memory flag only — never persisted, safe across concurrent users
        doc._attempt_nil_exempt_einvoice = True


def generate_einvoice_on_sales_return(doc, method):
    """
    Hook: on_submit on Sales Invoice.
    Attempt e-Invoice generation for nil/exempt sales returns.
    Temporarily overrides GST Settings only within this call,
    and always restores the original value.
    """
    if not _is_eligible_return(doc):
        return

    if not getattr(doc, "_attempt_nil_exempt_einvoice", False):
        return

    _generate_with_setting_override(doc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_eligible_return(doc):
    """Return True if this doc is a sales return that still needs an IRN."""
    return bool(doc.is_return and not doc.get("irn"))


def _generate_with_setting_override(doc):
    """
    Temporarily set nil_exempt_e_invoice_treatment to 'Generate',
    call generate_e_invoice, then unconditionally restore the original value.
    """
    setting_doctype = "GST Settings"
    setting_field = "nil_exempt_e_invoice_treatment"
    override_value = "Generate"

    original_value = frappe.db.get_single_value(setting_doctype, setting_field)

    try:
        if original_value != override_value:
            frappe.db.set_value(
                setting_doctype,
                setting_doctype,
                setting_field,
                override_value,
            )

        generate_e_invoice(doc.name, throw=False)

    except NotApplicableError:
        # Log instead of silently passing — helps with debugging
        frappe.log_error(
            title="e-Invoice: NotApplicableError on nil/exempt return",
            message=(
                f"Invoice {doc.name} raised NotApplicableError "
                f"during nil/exempt e-Invoice generation.\n"
                + frappe.get_traceback()
            ),
        )

    except Exception:
        frappe.log_error(
            title="e-Invoice generation failed for Sales Return",
            message=f"Invoice: {doc.name}\n" + frappe.get_traceback(),
        )

    finally:
        # Always restore — even if generate_e_invoice raised an unhandled error
        current_value = frappe.db.get_single_value(setting_doctype, setting_field)
        if current_value != original_value:
            frappe.db.set_value(
                setting_doctype,
                setting_doctype,
                setting_field,
                original_value,
            )
