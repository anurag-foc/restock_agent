// Badge only ships 4 variants (default/destructive/secondary/outline), which isn't enough
// to keep every urgency level or request status visually distinct, so these map straight
// to Tailwind classes instead of the variant prop.
export const URGENCY_BADGE_CLASS: Record<string, string> = {
  CRITICAL: 'border-transparent bg-red-600 text-white',
  HIGH: 'border-transparent bg-orange-500 text-white',
  MEDIUM: 'border-transparent bg-amber-400 text-amber-950',
  LOW: 'border-transparent bg-slate-200 text-slate-700',
};

export const DEFAULT_BADGE_CLASS = 'border-transparent bg-slate-200 text-slate-700';

export const STATUS_BADGE_CLASS: Record<string, string> = {
  PENDING_APPROVAL: 'border-transparent bg-slate-200 text-slate-700',
  NEEDS_REVIEW: 'border-transparent bg-amber-400 text-amber-950',
  APPROVED: 'border-transparent bg-emerald-500 text-white',
  REJECTED: 'border-transparent bg-red-600 text-white',
  FULFILLING: 'border-transparent bg-blue-500 text-white',
  COMPLETED: 'border-transparent bg-indigo-600 text-white',
};
