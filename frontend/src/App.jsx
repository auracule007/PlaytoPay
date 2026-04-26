import { useEffect, useState } from "react";
import { api } from "./api.js";
import BalanceCard from "./components/BalanceCard.jsx";
import PayoutForm from "./components/PayoutForm.jsx";
import PayoutHistory from "./components/PayoutHistory.jsx";
import LedgerHistory from "./components/LedgerHistory.jsx";

export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [merchantId, setMerchantId] = useState(null);
  const [balance, setBalance] = useState(null);
  const [ledger, setLedger] = useState([]);
  const [payouts, setPayouts] = useState([]);
  const [bankAccounts, setBankAccounts] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .listMerchants()
      .then((rows) => {
        setMerchants(rows);
        if (rows.length && !merchantId) setMerchantId(rows[0].id);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function refresh() {
    if (!merchantId) return;
    try {
      const [b, l, p, ba] = await Promise.all([
        api.getBalance(merchantId),
        api.getLedger(merchantId),
        api.listPayouts(merchantId),
        api.getBankAccounts(merchantId),
      ]);
      setBalance(b);
      setLedger(l);
      setPayouts(p);
      setBankAccounts(ba);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }

  // Whenever the selected merchant changes, fetch fresh state.
  useEffect(() => {
    refresh();
  }, [merchantId]);

  // Live updates: poll every 2s. Cheap, robust, no websockets needed for
  // this scale. Production would swap this for SSE or websockets when the
  // merchant has many in-flight payouts.
  useEffect(() => {
    if (!merchantId) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [merchantId]);

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Playto Pay</h1>
            <p className="text-xs text-slate-500">Merchant Payout Dashboard</p>
          </div>
          <select
            className="rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            value={merchantId || ""}
            onChange={(e) => setMerchantId(e.target.value)}
          >
            {merchants.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-6 px-6 py-8">
        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <BalanceCard balance={balance} />

        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <PayoutForm
            merchantId={merchantId}
            bankAccounts={bankAccounts}
            availablePaise={balance?.available_paise ?? 0}
            onCreated={refresh}
          />
          <LedgerHistory entries={ledger} />
        </div>

        <PayoutHistory payouts={payouts} />
      </main>
    </div>
  );
}
