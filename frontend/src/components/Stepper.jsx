const STEPS = ["Review plan", "Run fixes", "Review changes", "Pull request"];

export default function Stepper({ current }) {
  return (
    <ol className="stepper">
      {STEPS.map((label, i) => (
        <li key={label} className={i < current ? "done" : i === current ? "current" : ""}>
          <span className="step-num">{i < current ? "✓" : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}
