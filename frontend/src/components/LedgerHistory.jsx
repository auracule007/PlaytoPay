import { paiseToInr, formatDateTime } from "../format.js";

export default function LedgerHistory({ entries }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Recent credits & debits
      </h2>
      <ul className="mt-4 divide-y divide-slate-100">
        {entries.length === 0 ? (
          <li className="py-3 text-sm text-slate-500">No ledger activity yet.</li>
        ) : (
          entries.map((e) => (
            <li key={e.id} className="flex items-start justify-between py-2 text-sm">
              <div>
                <p className="font-medium">
                  {e.description || (e.kind === "CREDIT" ? "Customer payment" : "Payout")}
                </p>
                <p className="text-xs text-slate-500">{formatDateTime(e.created_at)}</p>
              </div>
              <p
                className={
                  e.kind === "CREDIT"
                    ? "font-semibold text-emerald-700"
                    : "font-semibold text-red-700"
                }
              >
                {e.kind === "CREDIT" ? "+" : "−"}
                {paiseToInr(e.amount_paise)}
              </p>
            </li>
          ))
        )}
      </ul>
    </section>
  );
}
