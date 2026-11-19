// Visible warning for a vSphere scan that did not see everything.
export default function ScanWarning({ title, text, problems = [] }) {
  return (
    <div className="warning-banner" role="alert">
      <strong>⚠ {title}</strong>
      {text}
      {problems.length > 0 && (
        <ul>
          {problems.map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      )}
    </div>
  )
}
