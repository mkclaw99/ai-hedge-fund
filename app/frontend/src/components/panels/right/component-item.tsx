import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { LucideIcon, Plus } from "lucide-react";
import { useState } from "react";

interface ComponentItemProps {
  icon: LucideIcon;
  label: string;
  description?: string;
  onClick?: () => void;
  className?: string;
  isActive?: boolean;
}

export default function ComponentItem({
  icon: Icon,
  label,
  description,
  onClick,
  className,
  isActive = false
}: ComponentItemProps) {
  const [isHovered, setIsHovered] = useState(false);

  const handlePlusClick = (e: React.MouseEvent) => {
    e.stopPropagation(); // Prevent triggering the parent onClick
    if (onClick) onClick();
  };

  const row = (
    <div
      className={cn(
        "group flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer text-subtitle transition-colors duration-150",
        isActive ? "bg-ramp-grey-700 text-primary" : "text-primary",
        isHovered ? "hover-bg" : "",
        className
      )}
      onClick={onClick}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && onClick) {
          onClick();
        }
      }}
    >
      <div className="flex-shrink-0">
        <Icon size={16} className={isActive ? "text-primary" : "text-muted-foreground"} />
      </div>
      <span className="truncate">{label}</span>

      {/* Add button using shadcn Button */}
      <div className="ml-auto opacity-0 group-hover:opacity-100">
        <Button
          variant="ghost"
          size="icon"
          className="h-5 w-5 hover-bg hover:text-primary text-muted-foreground flex items-center justify-center"
          onClick={handlePlusClick}
          aria-label={`Add ${label} to the flow`}
        >
          <Plus size={14} />
        </Button>
      </div>
    </div>
  );

  // Without a description there's nothing to explain — render the bare row.
  if (!description) {
    return row;
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{row}</TooltipTrigger>
      <TooltipContent side="left" className="max-w-xs">
        <p className="font-medium">{label}</p>
        <p className="mt-0.5 text-primary-foreground/80">{description}</p>
      </TooltipContent>
    </Tooltip>
  );
}
