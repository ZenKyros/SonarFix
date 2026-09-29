export default function DiffBlock({ diff, compact = false }) {
  if (!diff) return null;
  return (
    <pre className={`code-block diff-block${compact ? " compact" : ""}`}>
      {diff.split("\n").map((line, i) => {
        let cls = "";
        if (line.startsWith("+++") || line.startsWith("---")) cls = "diff-file";
        else if (line.startsWith("+")) cls = "diff-add";
        else if (line.startsWith("-")) cls = "diff-del";
        else if (line.startsWith("@@")) cls = "diff-hunk";
        return (
          <div key={i} className={cls}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}
