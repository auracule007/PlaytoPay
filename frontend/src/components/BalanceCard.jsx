import { paiseToInr } from "../format.js";

export default function BalanceCard({ balance }) {
  const available = balance?.available_paise ?? 0;
  const held = balance?.held_paise ?? 0;
  const settled = balance?.settled_paise ?? 0;

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
        <Stat label="Available" value={paiseToInr(available)} accent="emerald" />
        <Stat label="Held (pending payouts)" value={paiseToInr(held)} accent="amber" />
        <Stat label="Settled (credits − debits)" value={paiseToInr(settled)} accent="slate" />
      </div>
    </section>
  );
}

function Stat({ label, value, accent }) {
  const accents = {
    emerald: "text-emerald-700",
    amber: "text-amber-700",
    slate: "text-slate-700",
  };
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${accents[accent]}`}>{value}</p>
    </div>
  );
}
