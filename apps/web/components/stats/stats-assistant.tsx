"use client";

import { useState } from "react";
import { Bot } from "lucide-react";
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { StatsWorkspace } from "./stats-workspace";

export function StatsAssistant() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <div className="pointer-events-none fixed bottom-5 right-5 z-40">
        <Button
          variant="primary"
          size="md"
          className="pointer-events-auto rounded-full px-4 shadow-elevated"
          onClick={() => setOpen(true)}
        >
          <Bot size={14} />
          sika stats
        </Button>
      </div>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="w-[min(720px,100vw)]">
          <SheetHeader>
            <SheetTitle>sika stats</SheetTitle>
            <SheetDescription>
              Global assistant for NBA, MLB, NFL, Soccer, Tennis, and UFC queries.
            </SheetDescription>
          </SheetHeader>
          <SheetBody className="overflow-hidden">
            <StatsWorkspace compact />
          </SheetBody>
        </SheetContent>
      </Sheet>
    </>
  );
}
