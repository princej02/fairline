import { Suspense } from "react";
import { EventView } from "@/components/EventView";
import { Loading } from "@/components/ui";

export default function Page() {
  return (
    <Suspense fallback={<Loading what="Loading match" />}>
      <EventView />
    </Suspense>
  );
}
