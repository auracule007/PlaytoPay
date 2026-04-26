import { useState } from "react";
import { api } from "../api.js";
import { inrInputToPaise, paiseToInr } from "../format.js";

export default function PayoutForm({ merchantId, bankAccounts, availablePaise, onCreated }) {
  const [amount, setAmount] = useState("");
  const [bankId, setBankId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState(null);
  const [messageKind, setMessageKind] = useState("info");

  async function submit(e) {
    e.preventDefault();
    setMessage(null);
    const paise = inrInputToPaise(amount);
    if (paise === null || paise <= 0) {
      setMessageKind("error");
      setMessage("Enter a positive amount, e.g. 500 or 1234.50");
      return;
    }
    const bankAccountId = bankId || bankAccounts[0]?.id;
    if (!bankAccountId) {
      setMessageKind("error");
      setMessage("Select a bank account first");
      return;
    }
    setSubmitting(true);
    try {
      // A fresh idempotency key per submit click. If the network drops and
      // the user clicks again, they get a new attempt — that is the correct
      // user-facing semantic. Idempotency protects against retries from the
      // same intent (proxy retries, double-tap), not against a deliberate
      // resubmit by the user.
      const idempotencyKey = crypto.randomUUID();
      const payout = await api.createPayout(merchantId, {
        amount_paise: paise,
        bank_account_id: bankAccountId,
        idempotencyKey,
      });
      setMessageKind("success");
      setMessage(`Payout ${payout.id.slice(0, 8)}… created. Status: ${payout.status}`);
      setAmount("");
      onCreated?.();
    } catch (err) {
      setMessageKind("error");
      setMessage(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Request a payout
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        Available: {paiseToInr(availablePaise)}
      </p>
      <form onSubmit={submit} className="mt-4 space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Amount (INR)</label>
          <input
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="500.00"
            inputMode="decimal"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Bank account</label>
          <select
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            value={bankId || (bankAccounts[0]?.id ?? "")}
            onChange={(e) => setBankId(e.target.value)}
          >
            {bankAccounts.map((b) => (
              <option key={b.id} value={b.id}>
                {b.account_holder} · {b.ifsc} · ****{b.account_number_last4}
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? "Requesting…" : "Request payout"}
        </button>
        {message && (
          <p
            className={
              messageKind === "error"
                ? "rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
                : messageKind === "success"
                ? "rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700"
                : "text-xs text-slate-600"
            }
          >
            {message}
          </p>
        )}
      </form>
    </section>
  );
}
