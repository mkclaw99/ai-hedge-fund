import { Card, CardHeader } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { Handle, Position, useReactFlow } from '@xyflow/react';
import { Trash2 } from 'lucide-react';
import { ReactNode } from 'react';

export interface NodeShellProps {
  id: string;
  selected?: boolean;
  isConnectable?: boolean;
  icon: ReactNode;
  iconColor?: string;
  name: string;
  description?: string;
  children: ReactNode;
  hasLeftHandle?: boolean;
  hasRightHandle?: boolean;
  status?: string;
  width?: string;
}

export function NodeShell({
  id,
  selected,
  isConnectable,
  icon,
  iconColor,
  name,
  description,
  children,
  hasLeftHandle = true,
  hasRightHandle = true,
  status = 'IDLE',
  width = 'w-64',
}: NodeShellProps) {
  const isInProgress = status === 'IN_PROGRESS';
  const { deleteElements } = useReactFlow();
  return (
    <div
      className={cn(
        "react-flow__node-default group relative select-none cursor-pointer p-0 rounded-lg border border-node transition-all duration-200",
        width,
        !selected && "hover:border-node-hover hover:shadow-lg",
        selected && "border-node-selected shadow-xl",
        isInProgress && "node-in-progress"
      )}
      data-id={id}
      data-nodeid={id}
    >
      {isInProgress && (
        <div className="animated-border-container"></div>
      )}
      {hasLeftHandle && (
        <Handle
          type="target"
          position={Position.Left}
          className="w-3 h-3 rounded-full bg-gray-500 border-2 border-card absolute left-0 top-1/2 -translate-x-1/2 -translate-y-1/2 z-10 transition-all duration-200 hover:bg-gray-500 hover:w-4 hover:h-4 hover:shadow-[0_0_5px_2px_rgba(59,130,246,0.3)]"
          isConnectable={isConnectable}
        />
      )}
      <div className="overflow-hidden rounded-lg">
        <Card className="bg-node rounded-none overflow-hidden border-none">
          <CardHeader className="p-3 bg-node flex flex-row items-center space-x-2 rounded-t-sm">
            <div className={cn(
              "flex items-center justify-center h-8 w-8 rounded-lg text-primary flex-shrink-0",
              isInProgress ? "gradient-animation" : iconColor
            )}>
              {icon}
            </div>
            <div className="text-title font-semibold text-primary flex-1 min-w-0 truncate">
              {name || "Custom Component"}
            </div>
            <Tooltip delayDuration={300}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); deleteElements({ nodes: [{ id }] }); }}
                  aria-label="Delete node"
                  className="nodrag nopan flex-shrink-0 inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground opacity-0 transition-all hover:bg-red-500/10 hover:text-red-500 focus:opacity-100 group-hover:opacity-100"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">Delete node</TooltipContent>
            </Tooltip>
          </CardHeader>
          {description && (
            <div className="px-3 py-2 text-subtitle text-primary text-left">
              {description}
            </div>
          )}
          {children}
        </Card>
      </div>
      {hasRightHandle && (
        <Handle
          type="source"
          position={Position.Right}
          className="w-3 h-3 rounded-full bg-gray-500 border-2 border-card absolute right-0 top-1/2 translate-x-1/2 -translate-y-1/2 z-10 transition-all duration-200 hover:bg-gray-500 hover:w-4 hover:h-4 hover:shadow-[0_0_5px_2px_rgba(59,130,246,0.3)]"
          isConnectable={isConnectable}
        />
      )}
    </div>
  );
} 