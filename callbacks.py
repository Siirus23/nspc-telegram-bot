from aiogram.filters.callback_data import CallbackData


# ===== CHECKOUT FLOW =====

class CheckoutDeliveryCB(CallbackData, prefix="checkout_delivery"):
    method: str


class CheckoutBrowseCB(CallbackData, prefix="checkout_browse"):
    choice: str


class CheckoutAddressCB(CallbackData, prefix="checkout_address"):
    action: str


class CheckoutSimpleCB(CallbackData, prefix="checkout"):
    action: str


# ===== ADMIN PANEL =====

class AdminPanelCB(CallbackData, prefix="admin"):
    action: str

class PaymentReviewCB(CallbackData, prefix="pay"):
    action: str     # approve / reject
    invoice: str
class ShippingActionCB(CallbackData, prefix="ship"):
    action: str   # start / cancel
    invoice: str
