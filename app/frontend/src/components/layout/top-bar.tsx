import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { PanelBottom, PanelRight, Settings } from 'lucide-react';

interface TopBarProps {
  isRightCollapsed: boolean;
  isBottomCollapsed: boolean;
  onToggleRight: () => void;
  onToggleBottom: () => void;
  onSettingsClick: () => void;
}

export function TopBar({
  isRightCollapsed,
  isBottomCollapsed,
  onToggleRight,
  onToggleBottom,
  onSettingsClick,
}: TopBarProps) {
  return (
    <div className="absolute top-0 right-0 z-40 flex items-center gap-0 py-1 px-2 bg-panel/80">
      {/* Bottom Panel Toggle */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            onClick={onToggleBottom}
            className={cn(
              "h-8 w-8 p-0 text-muted-foreground hover:text-foreground hover:bg-ramp-grey-700 transition-colors",
              !isBottomCollapsed && "text-foreground"
            )}
            aria-label="Toggle bottom panel"
          >
            <PanelBottom size={16} />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          Show/hide the output &amp; terminal panel
          <span className="ml-2 text-primary-foreground/60">⌘J</span>
        </TooltipContent>
      </Tooltip>

      {/* Right Sidebar Toggle */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            onClick={onToggleRight}
            className={cn(
              "h-8 w-8 p-0 text-muted-foreground hover:text-foreground hover:bg-ramp-grey-700 transition-colors",
              !isRightCollapsed && "text-foreground"
            )}
            aria-label="Toggle right sidebar"
          >
            <PanelRight size={16} />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          Show/hide the components palette
          <span className="ml-2 text-primary-foreground/60">⌘I</span>
        </TooltipContent>
      </Tooltip>

      {/* Divider */}
      <div className="w-px h-5 bg-ramp-grey-700 mx-1" />

      {/* Settings */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            onClick={onSettingsClick}
            className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground hover:bg-ramp-grey-700 transition-colors"
            aria-label="Open settings"
          >
            <Settings size={16} />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          Settings — API keys, models, appearance
          <span className="ml-2 text-primary-foreground/60">⌘,</span>
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
