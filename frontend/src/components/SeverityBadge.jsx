const COLORS = {
  BLOCKER: "#8b0000",
  CRITICAL: "#d64545",
  MAJOR: "#e08e2b",
  MINOR: "#3b82c4",
  INFO: "#8a8f98",
};

export default function SeverityBadge({ severity }) {
  const color = COLORS[severity] || COLORS.INFO;
  return (
    <span className="badge" style={{ background: color }}>
      {severity}
    </span>
  );
}
