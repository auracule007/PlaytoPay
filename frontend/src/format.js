// Money formatting helpers. The backend speaks paise (integers). We format
// to INR with the Indian locale only at the UI boundary. We never do math
// on the formatted strings.

export function paiseToInr(paise) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
  }).format(paise / 100);
}

export function inrInputToPaise(value) {
  // Accepts strings like "100", "100.5", "1,200.75". Returns integer paise.
  if (value === null || value === undefined || value === "") return null;
  const cleaned = String(value).replace(/,/g, "").trim();
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  const [whole, frac = ""] = cleaned.split(".");
  const paddedFrac = (frac + "00").slice(0, 2);
  return parseInt(whole, 10) * 100 + parseInt(paddedFrac, 10);
}

export function formatStatus(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function formatDateTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}
