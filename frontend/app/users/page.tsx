"use client";

import { UsersRound } from "lucide-react";

import { Badge, EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui";
import { timeAgo, useApi, type ManagedUser, type UserStats } from "@/lib/api";

type UsersResponse = { stats: UserStats; users: ManagedUser[] };

export default function UsersPage() {
  const users = useApi<UsersResponse>("/api/users", 15000);

  return (
    <>
      <PageHeader title="Users" description="Accounts with access to this Clipper installation. Active means seen in the last 15 minutes." />
      {users.error ? (
        <ErrorState message={users.error} onRetry={users.reload} />
      ) : users.loading && !users.data ? (
        <SkeletonRows rows={5} />
      ) : !users.data?.users.length ? (
        <EmptyState icon={<UsersRound />} title="No users yet" body="Accounts appear here after they sign in with Google." />
      ) : (
        <div className="flex flex-col gap-4">
          <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line shadow-soft sm:max-w-md">
            <div className="bg-surface px-4 py-3">
              <dt className="text-xs text-ink-3">Active now</dt>
              <dd className="num mt-0.5 text-xl font-semibold">{users.data.stats.active_users}</dd>
            </div>
            <div className="bg-surface px-4 py-3">
              <dt className="text-xs text-ink-3">Total accounts</dt>
              <dd className="num mt-0.5 text-xl font-semibold">{users.data.stats.total_users}</dd>
            </div>
          </dl>

          <div className="panel overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="border-b border-line-subtle text-xs text-ink-3">
                <tr>
                  <th scope="col" className="px-4 py-2 text-left font-medium">User</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Status</th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">Role</th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">Last sign-in</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {users.data.users.map((user) => (
                  <tr key={user.id} className="hover:bg-raised">
                    <td className="px-4 py-3">
                      <p className="font-medium">{user.name}</p>
                      <p className="text-xs text-ink-3">{user.email}</p>
                    </td>
                    <td className="px-4 py-3"><Badge tone={user.active ? "ok" : "neutral"}>{user.active ? "Active" : "Offline"}</Badge></td>
                    <td className="px-4 py-3 text-ink-2">{user.is_admin ? "Administrator" : "Member"}</td>
                    <td className="px-4 py-3 text-right text-ink-2">{user.last_login_at ? timeAgo(user.last_login_at) : "Never"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
