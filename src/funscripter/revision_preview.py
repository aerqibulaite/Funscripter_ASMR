"""Static baseline/external/candidate comparison bound to a revision ID."""
from PIL import Image,ImageDraw
from .preview import font


def draw_comparison(path,baseline,external,duration,protected,revision_id,classification,candidate=None,locks=()):
    image=Image.new("RGB",(1600,900),"#f4f6fa")
    draw=ImageDraw.Draw(image)
    title,regular,small=font(30),font(20),font(16)
    draw.rectangle((0,0,1600,105),fill="#12233c")
    draw.text((40,20),"External revision / regeneration comparison",font=title,fill="white")
    draw.text((42,65),f"{classification}  |  revision {revision_id[:12]}  |  REVIEW REQUIRED",font=small,fill="#e8c98b")
    left,right,top,bottom=100,1540,175,660
    x=lambda time:left+time*(right-left)/max(1,duration)
    y=lambda pos:bottom-pos*(bottom-top)/100
    draw.rounded_rectangle((30,125,1570,715),radius=12,fill="white")
    for interval in protected:
        draw.rectangle((x(interval["start_ms"]),top,x(interval["end_ms"]),bottom),fill="#fff0d1")
    for interval in locks:
        draw.rectangle((x(interval["start_ms"]),top,x(interval["end_ms"]),bottom),fill="#f5d9df")
    for pos in range(0,101,25):
        yy=y(pos)
        draw.line((left,yy,right,yy),fill="#d9e0e9")
        draw.text((53,yy-10),str(pos),font=small,fill="#65758a")
    for index in range(7):
        time=duration*index/6
        xx=x(time)
        draw.line((xx,top,xx,bottom),fill="#e0e6ee")
        draw.text((xx-12,bottom+12),f"{time/1000:.2f}",font=small,fill="#65758a")
    for data,color,width in [(baseline,"#9ba7b6",4),(external,"#286ab1",3),(candidate,"#20865d",2)]:
        if data and data.get("actions"):
            points=data["actions"]
            coords=[(x(0),y(points[0]["pos"]))]+[(x(p["at"]),y(p["pos"])) for p in points]+[(x(duration),y(points[-1]["pos"]))]
            draw.line(coords,fill=color,width=width)
    draw.text((100,735),"Gray: baseline   Blue: external original   Green: candidate   Amber: diff protection   Rose: explicit lock",font=small,fill="#42556d")
    draw.text((100,774),"Protected supports include neighboring points from BOTH curves. Boundary anchors are retained.",font=regular,fill="#263e5c")
    draw.text((100,812),"This comparison shows saved curve data. Human edit intent and external player settings cannot be inferred from point similarity.",font=small,fill="#637188")
    draw.text((100,846),"Time: seconds  |  Position: normalized 0-100  |  Old diagnostic videos describe their original generated revision only.",font=small,fill="#637188")
    image.save(path)
