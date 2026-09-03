import { createBrowserRouter, RouterProvider, Outlet, useSearchParams, useLocation, Navigate, Link } from 'react-router';
import { useState, useEffect } from 'react';
import { Button, Sheet, SheetContent, SheetHeader, SheetTitle, useIsMobile } from '@databricks/appkit-ui/react';
import { Menu } from 'lucide-react';
import { cn } from './lib/utils';
import { PendingQuotesPage } from './pages/PendingQuotesPage';
import { QuoteDetailPage } from './pages/QuoteDetailPage';
import { FulfillingOrdersPage } from './pages/FulfillingOrdersPage';

const NAV_ITEMS = [
  { to: '/', label: 'Pending Quotes', isActive: (path: string) => path === '/' || path.startsWith('/quotes') },
  { to: '/fulfilling', label: 'Fulfilling Orders', isActive: (path: string) => path.startsWith('/fulfilling') },
];

function NavLinks({ className, onClick }: { className?: string; onClick?: () => void }) {
  const location = useLocation();

  return (
    <nav className={className}>
      {NAV_ITEMS.map(({ to, label, isActive }) => {
        const active = isActive(location.pathname);
        return (
          <Link
            key={to}
            to={to}
            onClick={onClick}
            className={cn(
              'px-3 py-1.5 rounded-md text-sm font-medium transition-colors',
              active ? 'bg-muted text-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

function Layout() {
  const isMobile = useIsMobile();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Close mobile nav when viewport crosses to desktop
  useEffect(() => {
    if (!isMobile) setMobileNavOpen(false);
  }, [isMobile]);

  return (
    <div className="min-h-screen bg-background flex flex-col">
      <header className="border-b px-4 md:px-6 py-3 flex items-center gap-4">
        <h1 className="text-lg font-semibold text-foreground">Inventory Intelligence Review</h1>
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
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
