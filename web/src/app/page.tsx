import { Suspense } from "react";
import { BoardView } from "@/components/BoardView";
import { Loading } from "@/components/ui";

export default function Page() {
  return (
    <Suspense fallback={<Loading what="Loading board" />}>
      <BoardView />
    </Suspense>
  );
}
