// Tiny API client. Reads VITE_API_BASE for production builds; falls back
// to "" so the dev-server proxy in vite.config.js can route /api to Django.

const API_BASE = import.meta.env.VITE_API_BASE || "";

async function request(path, { merchantId, method = "GET", body, headers = {} } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(merchantId ? { "X-Merchant-Id": merchantId } : {}),
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }

  if (!res.ok) {
    const message = data?.error?.message || `HTTP ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

export const api = {
  listMerchants: () => request("/api/v1/merchants"),
  getBalance: (merchantId) => request("/api/v1/balance", { merchantId }),
  getLedger: (merchantId) => request("/api/v1/ledger", { merchantId }),
  getBankAccounts: (merchantId) => request("/api/v1/bank-accounts", { merchantId }),
  listPayouts: (merchantId) => request("/api/v1/payouts", { merchantId }),
  createPayout: (merchantId, { amount_paise, bank_account_id, idempotencyKey }) =>
    request("/api/v1/payouts", {
      merchantId,
      method: "POST",
      body: { amount_paise, bank_account_id },
      headers: { "Idempotency-Key": idempotencyKey },
    }),
};
