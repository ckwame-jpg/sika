import { redirect } from "next/navigation";

export default async function DashboardRedirectPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const resolved = await searchParams;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(resolved)) {
    if (Array.isArray(value)) {
      value.forEach((entry) => params.append(key, entry));
    } else if (value != null) {
      params.set(key, value);
    }
  }
  const query = params.toString();
  redirect(query ? `/trade?${query}` : "/trade");
}
