import { ButtonLink, EmptyState } from "../components/Common";

export default function NotFound() {
  return (
    <main className="signin">
      <div className="card signin-card">
        <EmptyState icon="info" title="Page not found" body="This page doesn't exist."
          action={<ButtonLink to="/" variant="secondary">Back to home</ButtonLink>} />
      </div>
    </main>
  );
}
