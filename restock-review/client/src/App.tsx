import { createBrowserRouter, RouterProvider, Outlet, useSearchParams, Navigate, useLocation } from 'react-router';
import { useState, useEffect } from 'react';
import { Button, Sheet, SheetContent, SheetHeader, SheetTitle, useIsMobile } from '@databricks/appkit-ui/react';
import { Menu } from 'lucide-react';
import { PendingQuotesPage } from './pages/PendingQuotesPage';
import { QuoteDetailPage } from './pages/QuoteDetailPage';
import { FulfillingOrdersPage } from './pages/FulfillingOrdersPage';
import { SettingsPage } from './pages/SettingsPage';

function NavLinks({ className, onClick }: { className?: string; onClick?: () => void }) {
  const linkClass =
    'px-3 py-1.5 rounded-md text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors';
  return (
    <nav className={className}>
      <a href="/" onClick={onClick} className={linkClass}>
        Pending Quotes
      </a>
      <a href="/fulfilling" onClick={onClick} className={linkClass}>
        In Progress Actions
      </a>
      <a href="/settings" onClick={onClick} className={linkClass}>
        Settings
      </a>
    </nav>
  );
}

// Settings is the one page that renders dark. It is a configuration surface rather than a
// working queue, and the contrast marks it as somewhere you visit deliberately rather than
// somewhere decisions get made. AppKit ships the `.dark` token palette, so this only has to
// swap the class the rest of the app hardcodes to `light` on <html>.
const DARK_ROUTES = new Set(['/settings']);

const TITLES: Record<string, string> = {
  '/settings': 'Inventory Intelligence Settings',
};
const DEFAULT_TITLE = 'Inventory Intelligence Review';

function Layout() {
  const isMobile = useIsMobile();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const { pathname } = useLocation();

  const isDark = DARK_ROUTES.has(pathname);
  const title = TITLES[pathname] ?? DEFAULT_TITLE;

  // Toggled on the root element rather than scoped to a wrapper: a dark panel sitting under a
  // light header reads as a rendering fault, not a design. Restores `light` on the way out so
  // navigating away can never strand the rest of the app in a theme it defines no tokens for.
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', isDark);
    root.classList.toggle('light', !isDark);
    document.title = title;
    return () => {
      root.classList.remove('dark');
      root.classList.add('light');
      document.title = DEFAULT_TITLE;
    };
  }, [isDark, title]);

  // Close mobile nav when viewport crosses to desktop
  useEffect(() => {
    if (!isMobile) setMobileNavOpen(false);
  }, [isMobile]);

  return (
    <div className="min-h-screen bg-background flex flex-col">
      <header className="border-b px-4 md:px-6 py-3 flex items-center gap-4">
        <h1 className="text-lg font-semibold text-foreground">{title}</h1>
        {/* Desktop nav — hidden below md breakpoint */}
        <NavLinks className="hidden md:flex gap-1" />
        {/* Mobile nav — visible below md breakpoint */}
        <div className="ml-auto md:hidden">
          <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
            <Button variant="ghost" size="icon" onClick={() => setMobileNavOpen(true)}>
              <Menu className="h-5 w-5" />
              <span className="sr-only">Open navigation</span>
            </Button>
            <SheetContent side="left">
              <SheetHeader>
                <SheetTitle>Navigation</SheetTitle>
              </SheetHeader>
              <NavLinks className="flex flex-col gap-1" onClick={() => setMobileNavOpen(false)} />
            </SheetContent>
          </Sheet>
        </div>
      </header>

      <main className="flex-1 p-4 md:p-6">
        <Outlet />
      </main>
    </div>
  );
}

// The Teams Adaptive Card deep-links to `/?quote_id=<id>` (see
// build_review_app_url in agentic_restock.integrations.teams_webhook).
// Redirect that into the real route so both entry points work.
function RootRoute() {
  const [searchParams] = useSearchParams();
  const quoteId = searchParams.get('quote_id');
  if (quoteId) {
    return <Navigate to={`/quotes/${encodeURIComponent(quoteId)}`} replace />;
  }
  return <PendingQuotesPage />;
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <RootRoute /> },
      { path: '/quotes/:quoteId', element: <QuoteDetailPage /> },
      { path: '/fulfilling', element: <FulfillingOrdersPage /> },
      { path: '/settings', element: <SettingsPage /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
