const COLORS = {
  BLOCKER: "#8b0000",
  CRITICAL: "#d64545",
  MAJOR: "#e08e2b",
  MINOR: "#3b82c4",
  INFO: "#8a8f98",
};

// SonarQube's current UI shows severity as Blocker/High/Medium/Low/Info
// (its Clean Code "software quality" severity) rather than the older
// Blocker/Critical/Major/Minor/Info wording - the Sonar API issue payload
// still uses the old names internally, so this is display-only; filtering
// and data keep using the raw value (CRITICAL, MAJOR, ...) unchanged.
export const SEVERITY_LABEL = {
  BLOCKER: "Blocker",
  CRITICAL: "High",
  MAJOR: "Medium",
  MINOR: "Low",
  INFO: "Info",
};

export default function SeverityBadge({ severity }) {
  const color = COLORS[severity] || COLORS.INFO;
  return (
    <span className="badge" style={{ background: color }}>
      {SEVERITY_LABEL[severity] || severity}
    </span>
  );
}
