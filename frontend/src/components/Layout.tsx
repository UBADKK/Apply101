import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { DemoBanner } from './DemoBanner';

const NAV_ITEMS = [
  { to: '/', label: 'Overview', end: true },
  { to: '/profile', label: 'Profile', end: false },
  { to: '/jobs', label: 'Jobs', end: false },
  { to: '/matches', label: 'Matches', end: false },
];

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = () => {
    logout('manual');
    navigate('/login', { replace: true });
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <DemoBanner />
      <header className="app-header">
        <div className="app-header-inner">
          <NavLink to="/" className="brand" aria-label="Apply101 overview">
            <span className="brand-mark" aria-hidden="true">
              A
            </span>
            Apply101
          </NavLink>
          <nav aria-label="Main">
            <ul className="nav-list">
              {NAV_ITEMS.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.end} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
          <div className="user-menu">
            <span className="user-name" title={user?.mail}>
              {user?.name}
            </span>
            <button type="button" className="button button-ghost button-small" onClick={handleLogout}>
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="page" id="main">
        <Outlet />
      </main>
      <footer className="app-footer">Apply101 · match your profile against job postings with reasons you can check</footer>
    </div>
  );
}

/** Frame for the login and register pages. */
export function PublicLayout() {
  return (
    <div className="app-shell public-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <DemoBanner />
      <main className="page page-narrow" id="main">
        <div className="public-brand">
          <span className="brand-mark" aria-hidden="true">
            A
          </span>
          Apply101
        </div>
        <Outlet />
      </main>
    </div>
  );
}
