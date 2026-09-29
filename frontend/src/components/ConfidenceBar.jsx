export default function ConfidenceBar({ value }) {
  if (value === null || value === undefined) return null;
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  return (
    <div className="confidence-bar" title={`Confidence ${pct}%`}>
      <div className="confidence-track">
        <div className="confidence-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="confidence-label">{pct}%</span>
    </div>
  );
}
