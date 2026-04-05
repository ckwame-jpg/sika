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
      <div className="pointer-events-none fixed bottom-3 right-3 z-40 sm:bottom-5 sm:right-5">
        <Button
          variant="primary"
          size="md"
          className="pointer-events-auto h-9 rounded-full px-3 text-xs shadow-elevated sm:h-8 sm:px-4 sm:text-sm"
          onClick={() => setOpen(true)}
        >
          <Bot size={14} />
          sika stats
        </Button>
      </div>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="w-screen sm:w-[min(720px,100vw)]">
          <SheetHeader>
            <SheetTitle>sika stats</SheetTitle>
            <SheetDescription>
              Player stats assistant for NBA, NFL, and MLB.
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
