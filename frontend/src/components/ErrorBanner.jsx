import { Link } from "react-router-dom";

// Turns a raw API error string into something a person can act on.
// Every screen already renders its errors through this one component, so
// classifying here covers all of them - SonarQube, Git hosts, the AI engine,
// and the SonarFix backend itself - without touching each page.

// "POST /projects/x/sync-issues failed (400): <real message>" -> "<real message>"
function stripRequestPrefix(message) {
  return message.replace(/^[A-Z]+ \S+ failed \(\d+\):\s*/, "");
}

function serviceName(text) {
  if (/sonarfix api|lost connection to sonarfix/.test(text)) return "the SonarFix backend";
  if (/sonar/.test(text)) return "SonarQube";
  if (/bitbucket|azure|dev\.azure|github|gitlab|git (push|clone|fetch|pull)/.test(text)) return "the Git server";
  if (/anthropic|claude|api\.anthropic/.test(text)) return "the AI service";
  return "the server";
}

const NETWORK = /could not reach|cannot reach|getaddrinfo|connecterror|connection (refused|reset|aborted)|timed out|timeout|name or service not known|network is unreachable|failed to fetch|ssl/;
const AUTH = /\b(401|403)\b|unauthori[sz]ed|forbidden|invalid token|authentication failed|access denied/;
const REPO = /no local repository|set the (local )?repo|repository path|not a git repository|does not exist:/;
const DIRTY = /uncommitted changes/;

export function describeError(raw) {
  const message = stripRequestPrefix(String(raw));
  const text = message.toLowerCase();

  if (NETWORK.test(text)) {
    const service = serviceName(text);
    if (service === "the SonarFix backend") {
      return {
        tone: "network",
        title: "SonarFix isn't responding",
        summary: "This page can't talk to the SonarFix backend.",
        steps: [
          "Make sure SonarFix is still running (keep its window open).",
          "If it was closed, start it again, then reload this page.",
        ],
        retry: true,
        message,
      };
    }
    return {
      tone: "network",
      title: `Can't reach ${service}`,
      summary: `SonarFix couldn't connect to ${service} from this computer.`,
      steps: [
        "Connect to the corporate network or VPN.",
        "Check the address in Settings is correct.",
        "Then try again.",
      ],
      settings: true,
      retry: true,
      message,
    };
  }
  if (AUTH.test(text)) {
    return {
      tone: "auth",
      title: "Access was denied",
      summary: `${serviceName(text)} rejected the credentials SonarFix sent.`,
      steps: [
        "Open Settings and re-enter the token for that service.",
        "Make sure the token hasn't expired and has the needed permissions.",
      ],
      settings: true,
      message,
    };
  }
  if (REPO.test(text)) {
    return {
      tone: "repo",
      title: "No local copy of the repository",
      summary: "SonarFix needs the repository on this computer to read and fix the code.",
      steps: [
        "Go to Projects and set the local folder for this project, or let SonarFix clone it.",
        "Make sure the folder is a git clone of the branch Sonar analysed.",
      ],
      message,
    };
  }
  if (DIRTY.test(text)) {
    return {
      tone: "repo",
      title: "Your folder has unsaved changes",
      summary: "SonarFix needs a clean folder to start a fix branch.",
      steps: ["Commit or stash your changes, then try again."],
      retry: true,
      message,
    };
  }
  return {
    tone: "generic",
    title: "Something went wrong",
    summary: message,
    steps: [],
    message: null,
  };
}

const ICON = { network: "⚡", auth: "🔒", repo: "📁", generic: "!" };

export default function ErrorBanner({ message, onDismiss, onRetry }) {
  if (!message) return null;
  const info = describeError(message);
  const retry = onRetry || (info.retry ? () => window.location.reload() : null);

  return (
    <div className={`error-box error-${info.tone}`} role="alert">
      <span className="error-icon" aria-hidden="true">{ICON[info.tone]}</span>
      <div className="error-main">
        <strong className="error-title">{info.title}</strong>
        <p className="error-summary">{info.summary}</p>
        {info.steps.length > 0 && (
          <>
            <span className="error-label">What to do</span>
            <ol className="error-steps">
              {info.steps.map((step) => <li key={step}>{step}</li>)}
            </ol>
          </>
        )}
        <div className="error-actions">
          {retry && <button className="btn btn-primary btn-sm" onClick={retry}>Try again</button>}
          {info.settings && <Link className="btn btn-outline btn-sm" to="/settings">Open Settings</Link>}
        </div>
        {info.message && (
          <details className="error-details">
            <summary>Technical details</summary>
            <code>{info.message}</code>
          </details>
        )}
      </div>
      {onDismiss && (
        <button className="banner-close" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  );
}
