"""Klien Midtrans Core API khusus pembayaran QRIS."""
import base64
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

PAID_STATUSES = {"settlement", "capture"}
FAILED_STATUSES = {"expire", "cancel", "deny", "failure"}


class MidtransError(Exception):
    pass


class MidtransClient:
    def __init__(self, server_key: str, is_production: bool = False, timeout: float = 20):
        self.base_url = "https://api.midtrans.com" if is_production else "https://api.sandbox.midtrans.com"
        token = base64.b64encode(f"{server_key}:".encode()).decode()
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        }
        self._timeout = timeout

    async def _request(self, method: str, path: str, json: Optional[dict] = None) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=self._headers,
                                         timeout=self._timeout) as client:
                resp = await client.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise MidtransError(f"Gagal menghubungi Midtrans: {e}") from e
        try:
            return resp.json()
        except ValueError:
            raise MidtransError(f"Respons Midtrans tidak valid (HTTP {resp.status_code})")

    async def create_qris_charge(self, order_id: str, gross_amount: int, items: list[dict],
                                 expiry_minutes: int = 10, acquirer: str = "gopay",
                                 customer: Optional[dict] = None) -> dict:
        body = {
            "payment_type": "qris",
            "transaction_details": {"order_id": order_id, "gross_amount": int(gross_amount)},
            "item_details": items,
            "qris": {"acquirer": acquirer},
            "custom_expiry": {"expiry_duration": expiry_minutes, "unit": "minute"},
        }
        if customer:
            body["customer_details"] = customer

        data = await self._request("POST", "/v2/charge", body)
        if str(data.get("status_code")) not in ("200", "201"):
            detail = data.get("validation_messages") or ""
            raise MidtransError(f"{data.get('status_code')} {data.get('status_message')} {detail}".strip())

        qr_url = next((a.get("url") for a in data.get("actions", []) if a.get("name") == "generate-qr-code"), None)
        return {
            "transaction_id": data.get("transaction_id"),
            "qr_string": data.get("qr_string"),
            "qr_url": qr_url,
            "expiry_time": data.get("expiry_time"),
            "raw": data,
        }

    async def get_status(self, order_id: str) -> dict:
        return await self._request("GET", f"/v2/{order_id}/status")

    async def expire(self, order_id: str) -> dict:
        """Batalkan transaksi yang masih pending."""
        return await self._request("POST", f"/v2/{order_id}/expire")
