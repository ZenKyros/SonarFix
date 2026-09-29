export default function BeforeAfter({ before, after }) {
  return (
    <div className="before-after">
      <div>
        <span className="ba-label ba-before">Before</span>
        <pre className="code-block compact">{before}</pre>
      </div>
      <div>
        <span className="ba-label ba-after">After</span>
        <pre className="code-block compact">{after}</pre>
      </div>
    </div>
  );
}
