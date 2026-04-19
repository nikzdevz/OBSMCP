import { Link } from 'react-router-dom';

export default function NotFound(): JSX.Element {
  return (
    <div className="card mx-auto mt-10 max-w-md text-center">
      <h2 className="text-xl font-semibold">Page not found</h2>
      <p className="mt-2 text-sm text-slate-600">That route doesn't exist.</p>
      <Link to="/" className="btn-primary mt-4 inline-block">
        Back to dashboard
      </Link>
    </div>
  );
}
