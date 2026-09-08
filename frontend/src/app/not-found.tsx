export default function NotFound() {
  return (
    <div className="notice" role="status" style={{ marginTop: "2rem" }}>
      <h1 className="notice__title">Page not found</h1>
      <p>
        That page doesn’t exist. <a href="/">Back to the latest stories</a>.
      </p>
    </div>
  );
}
