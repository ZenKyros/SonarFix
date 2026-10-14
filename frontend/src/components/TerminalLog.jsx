// A visible (not collapsed) terminal-style panel for build/test command
// output - the actual commands SonarFix ran and what they printed, so a
// reviewer can see the real "did it build, did it test" evidence instead of
// trusting a pass/fail badge alone.
export default function TerminalLog({ title, output }) {
  if (!output) return null;
  return (
    <div className="terminal">
      <div className="terminal-bar">
        <span className="terminal-dot terminal-dot-red" />
        <span className="terminal-dot terminal-dot-yellow" />
        <span className="terminal-dot terminal-dot-green" />
        <span className="terminal-title">{title}</span>
      </div>
      <pre className="terminal-body">{output}</pre>
    </div>
  );
}
