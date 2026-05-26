import { useLayoutContext } from '@/contexts/layout-context';
import { useResizable } from '@/hooks/use-resizable';
import { cn } from '@/lib/utils';
import { AlertCircle, Bug, FileText, Terminal, X } from 'lucide-react';
import { ReactNode, useEffect } from 'react';
import { Button } from '../../ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../ui/tabs';
import { Tooltip, TooltipContent, TooltipTrigger } from '../../ui/tooltip';
import { DebugConsoleTab, OutputTab, ProblemsTab, TerminalTab } from './tabs';

interface BottomPanelProps {
  children?: ReactNode;
  isCollapsed: boolean;
  onCollapse: () => void;
  onExpand: () => void;
  onToggleCollapse: () => void;
  onHeightChange?: (height: number) => void;
}

const TAB_TRIGGER_CLASS =
  "flex items-center gap-2 px-3 py-1.5 text-sm data-[state=active]:active-item text-muted-foreground";

export function BottomPanel({
  isCollapsed,
  onToggleCollapse,
  onHeightChange,
}: BottomPanelProps) {
  const { currentBottomTab, setBottomPanelTab } = useLayoutContext();

  // Use our custom hooks for vertical resizing
  const { height, isDragging, elementRef, startResize } = useResizable({
    defaultHeight: 300,
    minHeight: 200,
    maxHeight: window.innerHeight,
    side: 'bottom',
  });

  // Notify parent component of height changes
  useEffect(() => {
    onHeightChange?.(height);
  }, [height, onHeightChange]);

  if (isCollapsed) {
    return null;
  }

  return (
    <div
      ref={elementRef}
      className={cn(
        "bg-panel flex flex-col relative border-t",
        isDragging ? "select-none" : ""
      )}
      style={{
        height: `${height}px`,
      }}
    >
      {/* Resize handle - on the top for bottom panel */}
      {!isDragging && (
        <div
          className="absolute top-0 left-0 right-0 h-1 cursor-ns-resize transition-all duration-150 z-10 hover-bg"
          onMouseDown={startResize}
        />
      )}

      {/* Header with tabs and close button */}
      <div className="flex items-center justify-between border-b px-4 py-2">
        <Tabs value={currentBottomTab} onValueChange={setBottomPanelTab} className="flex-1">
          <div className="flex items-center justify-between">
            <TabsList className="bg-transparent border-none p-0 h-auto">
              <Tooltip>
                <TooltipTrigger asChild>
                  <TabsTrigger value="output" className={TAB_TRIGGER_CLASS}>
                    <FileText size={14} />
                    Output
                  </TabsTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs">
                  Results from the latest run — agent progress, a decisions summary, and per-ticker analysis.
                </TooltipContent>
              </Tooltip>

              <Tooltip>
                <TooltipTrigger asChild>
                  <TabsTrigger value="terminal" className={TAB_TRIGGER_CLASS}>
                    <Terminal size={14} />
                    Terminal
                  </TabsTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs">
                  Raw console output from runs.
                </TooltipContent>
              </Tooltip>

              <Tooltip>
                <TooltipTrigger asChild>
                  <TabsTrigger value="problems" className={TAB_TRIGGER_CLASS}>
                    <AlertCircle size={14} />
                    Problems
                  </TabsTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs">
                  Configuration and validation issues with the current flow.
                </TooltipContent>
              </Tooltip>

              <Tooltip>
                <TooltipTrigger asChild>
                  <TabsTrigger value="debug" className={TAB_TRIGGER_CLASS}>
                    <Bug size={14} />
                    Debug Console
                  </TabsTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs">
                  Detailed debug logs and diagnostics from runs.
                </TooltipContent>
              </Tooltip>
            </TabsList>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={onToggleCollapse}
                  className="h-6 w-6 text-primary hover-bg"
                  aria-label="Close panel"
                >
                  <X size={14} />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top">
                Hide panel
                <span className="ml-2 text-primary-foreground/60">⌘J</span>
              </TooltipContent>
            </Tooltip>
          </div>
        </Tabs>
      </div>

      {/* Content area */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <Tabs value={currentBottomTab} className="h-full">
          <TabsContent value="output" className="h-full m-0 p-4">
            <OutputTab className="h-full" />
          </TabsContent>
          <TabsContent value="terminal" className="h-full m-0 p-4">
            <TerminalTab className="h-full" />
          </TabsContent>
          <TabsContent value="problems" className="h-full m-0 p-4">
            <ProblemsTab className="h-full" />
          </TabsContent>
          <TabsContent value="debug" className="h-full m-0 p-4">
            <DebugConsoleTab className="h-full" />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
