export default function StatTile({ label, value, hint, tone = "neutral" }) {
  return (
    <div className={`stat tone-${tone}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}
