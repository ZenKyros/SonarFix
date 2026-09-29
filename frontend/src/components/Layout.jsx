import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { api } from "../api";

export default function Layout({ children }) {
  const [wiring, setWiring] = useState(null);

  useEffect(() => {
    api
      .health()
      .then((res) => setWiring(res.engine))
      .catch(() => setWiring(null));
  }, []);

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">SF</span>
          <span>SonarFix</span>
        </Link>
        {wiring && <code className="wiring">{wiring}</code>}
      </header>
      <main className="content">{children}</main>
    </div>
  );
}
