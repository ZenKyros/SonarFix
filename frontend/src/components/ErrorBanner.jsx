export default function ErrorBanner({ message, onDismiss }) {
  if (!message) return null;
  return (
    <div className="banner banner-error">
      <span>{message}</span>
      {onDismiss && (
        <button className="banner-close" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  );
}
