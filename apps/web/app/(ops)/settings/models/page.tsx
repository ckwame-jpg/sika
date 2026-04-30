import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Header } from "@/components/layout/header";
import { ModelReadinessPanel } from "@/components/predictions/model-readiness-panel";
import { Button } from "@/components/ui/button";

export default function SettingsModelsPage() {
  return (
    <>
      <Header
        title="Model Readiness"
        actions={(
          <Button variant="ghost" size="sm" asChild>
            <Link href="/settings" className="flex items-center gap-1">
              <ArrowLeft size={12} />
              Settings
            </Link>
          </Button>
        )}
      />
      <main className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-6xl">
          <ModelReadinessPanel />
        </div>
      </main>
    </>
  );
}
