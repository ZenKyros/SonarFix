import { useStatus } from "../status";

// A slim, always-visible strip of whatever SonarFix is doing right now -
// cloning, fetching issues, asking Claude to analyze, applying a fix, etc.
// It only renders while something is actually in flight.
export default function ActivityBar() {
  const { tasks } = useStatus();
  if (tasks.length === 0) return null;

  const [primary, ...rest] = tasks;

  return (
    <div className="activity-bar" role="status">
      <span className="activity-spinner" />
      <span className="activity-label">{primary.label}</span>
      {primary.detail && <span className="activity-detail">{primary.detail}</span>}
      {rest.length > 0 && <span className="activity-more">+{rest.length} more</span>}
    </div>
  );
}
