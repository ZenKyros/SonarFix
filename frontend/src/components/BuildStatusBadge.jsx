const ICON = { passed: "✓", failed: "✕", skipped: "⚠" };
const CLASS = { passed: "status-badge-pass", failed: "status-badge-fail", skipped: "status-badge-skip" };
const DEFAULT_LABEL = {
  passed: "Passed",
  failed: "Failed",
  skipped: "Not verified",
};

// A single check/cross pill for a build or test result - `kind` is just the
// leading word ("Build" / "Test") so the two read as a pair side by side.
export default function BuildStatusBadge({ status, kind }) {
  if (!status) return null;
  return (
    <span className={`status-badge ${CLASS[status] || ""}`}>
      <span className="status-badge-icon" aria-hidden="true">
        {ICON[status] || "•"}
      </span>
      {kind ? `${kind}: ` : ""}
      {DEFAULT_LABEL[status] || status}
    </span>
  );
}
