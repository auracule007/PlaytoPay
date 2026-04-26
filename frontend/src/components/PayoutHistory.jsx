import { paiseToInr, formatDateTime, formatStatus } from "../format.js";

const STATUS_STYLES = {
  pending: "bg-slate-100 text-slate-700",
  processing: "bg-amber-100 text-amber-800",
  completed: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
};

export default function PayoutHistory({ payouts }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Payout history
      </h2>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase tracking-wide text-slate-500">
            <tr className="border-b border-slate-200">
              <th className="px-2 py-2">When</th>
              <th className="px-2 py-2">Amount</th>
              <th className="px-2 py-2">Status</th>
              <th className="px-2 py-2">Attempts</th>
              <th className="px-2 py-2">Bank</th>
              <th className="px-2 py-2">ID</th>
            </tr>
          </thead>
          <tbody>
            {payouts.length === 0 ? (
              <tr>
                <td className="px-2 py-4 text-slate-500" colSpan={6}>
                  No payouts yet.
                </td>
              </tr>
            ) : (
              payouts.map((p) => (
                <tr key={p.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-2 py-2 text-slate-600">{formatDateTime(p.created_at)}</td>
                  <td className="px-2 py-2 font-medium">{paiseToInr(p.amount_paise)}</td>
                  <td className="px-2 py-2">
                    <span
                      className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${
                        STATUS_STYLES[p.status] || "bg-slate-100 text-slate-700"
                      }`}
                    >
                      {formatStatus(p.status)}
                    </span>
                    {p.failure_reason && (
                      <span className="ml-2 text-xs text-red-600">
                        {p.failure_reason}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-slate-600">{p.attempts}</td>
                  <td className="px-2 py-2 text-slate-600">
                    ****{p.bank_account?.account_number_last4}
                  </td>
                  <td className="px-2 py-2 font-mono text-xs text-slate-400">
                    {p.id.slice(0, 8)}…
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
