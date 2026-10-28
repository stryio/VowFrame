"""Built-in prompt data and reproducible V3 prompt composition."""

import re


SAME_LOOK = '同一组照片里，两人的服装、发型、妆容和配饰保持完全一致。'

_STYLE_DATA = (
    {
        'id': 'korean_minimal',
        'name': '韩系极简',
        'description': '白纱黑西装、干净影棚、通透柔光',
        'costume': '新娘：干净的低盘发，额前留少量自然碎发；清透的伪素颜妆，柔和的豆沙色唇妆；珍珠耳钉，一条及肩的单层短头纱；简约的白色缎面婚纱，方领、无袖、收腰，裙身没有蕾丝和亮片，裙摆垂顺及地。新郎：干净利落的短发；黑色修身西装，白衬衫，黑色细领带。',
        'look': '画面干净克制，大面积留白，低饱和的暖白色调，柔和通透的散射光，阴影很浅。',
        'variants': (
            ('落地窗影棚', '浅米白色影棚，一整面落地窗透进柔和的日光，地面有淡淡的窗格光影。'),
            ('白色纱幔', '从天花板垂下几层半透明的白色纱幔，窗光透过纱幔变得更柔和，可以隔着纱幔作前景拍摄。'),
            ('弧形白墙', '一面米白色的弧形墙，墙前有一张简洁的白色木凳和一束白色花材，柔光从侧前方的大窗照来。'),
            ('白色台阶', '几级宽大的白色台阶，两人可以坐或站在不同高度，顶部天窗的柔光均匀洒下。'),
        ),
        'avoid': '杂乱背景、强烈阴影、过曝发白',
    },
    {
        'id': 'french_vintage',
        'name': '法式复古',
        'description': '复古礼服、巴黎街角、胶片色调',
        'costume': '新娘：法式低盘卷发，颅顶蓬松，耳侧垂下少量卷发；清透底妆，柔粉色眼妆，玫瑰豆沙色唇妆；珍珠耳坠，一条及地的轻薄长头纱；复古法式婚纱，象牙白缎面结合大面积立体蕾丝，蕾丝长袖、V 领，裙摆层叠并带拖尾。新郎：保持参考照片中的发型轮廓和发际线，打理得干净有型；深色双排扣西装，白衬衫，深色领结，胸前别一朵白色小花。',
        'look': '浪漫松弛的巴黎氛围，暖调的复古胶片色彩与轻微颗粒，人物写实。',
        'variants': (
            ('咖啡馆街角', '咖啡馆的遮阳棚、门口的小圆桌和藤椅、石板路，下午斜射的暖阳在石墙上投出长长的影子。'),
            ('花店门口', '街边花店门口摆满一桶桶鲜花，午后的阳光被遮阳棚挡去一半，光线柔和。'),
            ('铁艺阳台', '奶油色的石砌公寓楼，铁艺阳台上挂着绿植，夕阳的侧光掠过墙面。'),
            ('塞纳河畔', '河边的石栏杆和梧桐树，远处是一座石桥，傍晚的暖光从侧后方照来。'),
        ),
        'avoid': '油画笔触、涂抹感、过度柔焦、发黄偏色',
    },
    {
        'id': 'forest_natural',
        'name': '森系自然',
        'description': '草地、树林、林间阳光',
        'costume': '新娘：松散的侧编发，头戴不遮挡额头的小花环；清透的自然妆，淡粉色腮红；细链珍珠耳坠，不戴头纱；轻纱婚纱，V 领、薄纱泡泡短袖，裙身点缀细小的立体花朵，多层轻纱裙摆及地。新郎：自然蓬松的短发；浅灰色西装，白衬衫，不打领带，胸前别一小束野花。',
        'look': '清新自然的空气感，嫩绿与暖金色调，阳光透过树叶，柔和的环境光照亮人物正面。',
        'variants': (
            ('林边草地', '树林边的草地上散落着小野花，阳光从侧后方透过树叶，形成金色的轮廓光。'),
            ('大树下', '一棵大树粗壮的树干和低垂的枝叶，树荫外是明亮的草地，草地反射的柔光照亮人物。'),
            ('林间小径', '一条铺满落叶的小径通向树林深处，两侧树干形成纵深，光束从树冠间斜射下来。'),
            ('林间溪边', '浅浅的林间小溪和溪边的石头，水面反射着天光。'),
        ),
        'avoid': '树叶斑驳阴影落在脸上、过曝、画面偏绿',
    },
    {
        'id': 'seaside_sunset',
        'name': '海边日落',
        'description': '海岸、晚霞、黄金时刻',
        'costume': '新娘：半扎的长卷发，发尾被海风吹起；清透的妆容，暖杏色唇妆；贝壳珍珠耳坠，一条随风飘起的长头纱；轻盈飘逸的雪纺婚纱，细肩带、V 领、露背，多层雪纺裙摆随风飘动，长度及踝。新郎：自然的短发；白衬衫，领口敞开一颗扣子，袖口挽起，浅米色西裤。',
        'look': '温暖浪漫的黄金时刻，暖橙与粉紫的天空色彩，夕阳形成暖色轮廓光，天空的余晖作为柔和的正面光。',
        'variants': (
            ('浪花沙滩', '湿润反光的沙滩和涌上来的浪花，夕阳低垂在海平面上方，沙滩上倒映着晚霞。'),
            ('海边礁石', '一段深色的礁石，海浪拍在石头上溅起水花，夕阳从侧面照来。'),
            ('海上栈道', '一条伸向海面的木栈道，两侧是海水，晚霞倒映在水面上。'),
            ('芒草沙丘', '海边长着芒草的沙丘，草叶在风里摆动，在逆光下发着金光。'),
        ),
        'avoid': '剪影、逆光下脸部发黑、过度橙红偏色',
    },
    {
        'id': 'chinese_traditional',
        'name': '中式国风',
        'description': '秀禾服、传统中式庭院',
        'costume': '新娘：光洁的中式低发髻，佩戴简洁的金色发簪和少量红色绒花，不戴凤冠，额头和脸部完整露出；端庄的淡妆，正红色唇妆；金色耳坠；红色秀禾服，立领、宽袖，衣身与裙摆满绣金色龙凤和祥云纹样。新郎：干净的短发；中式长袍马褂，红色长袍配黑色马褂，马褂上有暗纹团花。',
        'look': '喜庆端庄，中国红与金色点缀，柔和的白天天光。',
        'variants': (
            ('朱红大门', '庭院的朱红大门、高高的门槛和门前的石鼓，柔和的天光。'),
            ('雕花窗棂', '雕花木窗前，阳光透过窗棂在墙上和人身上投出花窗光影。'),
            ('灯笼回廊', '长长的木回廊和立柱，檐下挂着一排红灯笼，形成纵深。'),
            ('青砖庭院', '庭院里的石阶、古树和青砖地，屋檐下柔和的光线。'),
        ),
        'avoid': '凤冠流苏遮挡面部、浓重戏剧妆、廉价影楼古装感、头饰堆叠',
    },
    {
        'id': 'cinematic_light',
        'name': '高级光影',
        'description': '光影塑形、明暗对比、背景简洁',
        'costume': '新娘：光洁的低盘发，没有碎发；干净的底妆，大地色眼妆，裸色唇妆；简洁的钻石耳钉，不戴头纱；极简缎面婚纱，一字肩、收腰，没有任何蕾丝和装饰，缎面在光下有柔和的光泽，裙摆垂顺及地。新郎：保持参考照片中的发型轮廓和发际线，打理整齐；黑色戗驳领礼服，白衬衫，黑色领结。',
        'look': '光和影是画面的主角：方向明确的光在墙面、地面和人物身上形成清晰的明暗结构或投影图案；背景简洁，颜色服务于光影；灯具等光源本身不出现在画面里。',
        'variants': (
            ('窗格投影', '暖白色墙面的空房间，午后的硬质阳光从一侧的窗户射入，在墙上、地面和人身上投出清晰的窗格影子。'),
            ('百叶窗条纹', '浅灰色的墙面，阳光穿过百叶窗，在墙面和两人身上落下斜向的条纹光影。'),
            ('树影墙', '米黄色的外墙，阳光把附近树枝和树叶的影子投在墙上和人身上。'),
            ('暗调单光', '深色背景，一束侧前方的柔光让人物从暗部中浮现，背景逐渐沉入暗部。'),
            ('色彩墙侧光', '墨绿色或雾蓝色的纯色墙面，一束侧光从画面一侧掠过墙面，形成由亮到暗的渐变。'),
        ),
        'avoid': '脸部大面积阴影、半边脸死黑、条纹或树影横跨眼睛、剪影、过度高反差',
    },
    {
        'id': 'new_chinese',
        'name': '新中式',
        'description': '现代东方美学、月洞门、竹影、素雅',
        'costume': '新娘：素雅的低发髻，插一支白玉发簪；清淡自然的妆容，柔和的藕粉色唇妆；白玉耳坠；米白色改良旗袍式婚纱，立领、盘扣，真丝面料带浅浅的暗纹，修身裙身下接轻纱裙摆。新郎：干净的短发；黛青色立领中式西装，盘扣，内搭白色立领衬衫。',
        'look': '现代简约的东方美学，素雅的米白、黛青与原木色，大面积留白，柔和的自然光。',
        'variants': (
            ('月洞门', '白墙上的圆形月洞门，门后是一片竹林，柔和的天光。'),
            ('竹影白墙', '白墙前种着一丛竹子，阳光把竹影投在墙上和人身上。'),
            ('原木茶室', '原木茶室里的矮桌、茶具和宣纸窗，窗光柔和。'),
            ('水边石径', '浅水池边的石板小径，水面倒映着白墙和天光。'),
        ),
        'avoid': '古装戏服感、浓重戏剧妆、头饰堆叠、廉价影楼感',
    },
    {
        'id': 'hongkong_retro',
        'name': '港风复古',
        'description': '90 年代港片色调、老街、霓虹',
        'costume': '新娘：90 年代港风的侧分大波浪卷发；复古妆容，正红色唇妆；珍珠耳环，一条蓬松的短头纱；复古缎面婚纱，泡泡袖、方领、收腰，裙长及小腿。新郎：保持参考照片中的发型轮廓和发际线，打理整齐并带一点发蜡光泽；宽肩的深棕色复古西装，宽驳领，白衬衫，复古花纹领带。',
        'look': '上世纪 90 年代港片般的怀旧氛围，偏暖的胶片色调和轻微颗粒，复古的红绿色彩对比。',
        'variants': (
            ('老街招牌', '密集的竖排招牌和旧唐楼，傍晚的天光与刚亮起的霓虹混在一起。'),
            ('电车站', '老式的双层电车和电车站，午后的阳光斜照在街上。'),
            ('茶餐厅', '茶餐厅的卡座、绿色瓷砖墙和吊扇，窗外的日光照进来。'),
            ('霓虹小巷', '夜晚的小巷，霓虹招牌的红光和蓝光落在两人身上，店铺门口的暖光照亮两人正面。'),
        ),
        'avoid': '脸部偏红或偏绿、画面过暗、噪点过多',
    },
    {
        'id': 'city_night',
        'name': '城市夜景',
        'description': '天台、城市灯火、虚化光斑',
        'costume': '新娘：精致的低盘发，耳侧留少量卷发；亮面底妆，香槟色眼妆，玫瑰色唇妆；闪亮的钻石耳坠和细链锁骨链；带细闪的缎面婚纱，吊带、心形领口，裙身缀满细碎亮片，在灯光下闪烁，A 字裙摆及地。新郎：保持参考照片中的发型轮廓和发际线，打理整齐；黑色修身西装，白衬衫，黑色领结。',
        'look': '现代都市的浪漫夜晚，深蓝的夜空与暖金色的城市灯火，背景是柔和的虚化光斑，人物由近处的暖光照亮。',
        'variants': (
            ('城市天台', '高楼天台的栏杆边，身后是城市天际线和蓝调时刻的天空，天台上的暖色串灯照亮两人。'),
            ('江边步道', '江边的步道，对岸高楼的灯火倒映在水面上，路灯的暖光照亮两人。'),
            ('街头光斑', '夜晚的商业街，背景的车灯和霓虹虚化成大片光斑，橱窗透出的暖光照亮两人。'),
            ('桥上车流', '城市大桥的人行道，身后是车流的光轨和城市灯火，桥上路灯的暖光照亮两人。'),
        ),
        'avoid': '脸部发黑、噪点过多、背景过曝、肤色偏黄',
    },
    {
        'id': 'flower_field',
        'name': '花海草原',
        'description': '花田、草原、开阔天空',
        'costume': '新娘：自然的半扎长发，发间别几朵小花；清透的素颜感妆容，淡粉色唇妆；小巧的珍珠耳钉，一条及腰的轻纱头纱；轻盈的薄纱婚纱，一字肩、短泡泡袖，多层薄纱裙摆及踝，裙身点缀细小的刺绣花朵。新郎：自然蓬松的短发；浅米色西装，白衬衫，不打领带。',
        'look': '开阔、自由、明亮，大片的花海或草地与天空，柔和的晴天光线和清新的色彩。',
        'variants': (
            ('薰衣草花田', '一垄垄紫色的薰衣草延伸到远处，午后的侧光。'),
            ('向日葵田', '齐腰高的向日葵和金黄的花盘，阳光从侧后方照来。'),
            ('辽阔草原', '起伏的绿色草原和蓝天白云，远处有一棵孤树，傍晚柔和的低角度阳光。'),
            ('油菜花田', '大片金黄的油菜花和远处的山丘，晴天柔和的光线。'),
        ),
        'avoid': '花朵遮挡面部、颜色过度饱和、过曝',
    },
    {
        'id': 'snow_winter',
        'name': '冬日雪景',
        'description': '雪地、松林、冬日暖阳',
        'costume': '新娘：优雅的低盘发；清透的底妆，带一点冷粉色腮红，酒红色唇妆；珍珠耳坠，一条及地长头纱；长袖缎面婚纱，船领，披一条白色毛绒披肩，裙摆及地带短拖尾。新郎：整齐的短发；深色长款羊毛大衣，内搭深色西装、白衬衫和深色领带，系一条格纹围巾。',
        'look': '纯净安静的冬日，大面积白雪与深色松林的对比，冷色调的环境里有暖色的阳光照在人物身上。',
        'variants': (
            ('雪中松林', '积雪的松林，阳光从树间斜射进来，空中飘着细雪。'),
            ('开阔雪原', '开阔的雪原和远处的雪山，低角度的冬日暖阳照在两人身上。'),
            ('雪中木屋', '雪中的小木屋，窗户透出暖黄的灯光，傍晚蓝调的天色。'),
            ('冰湖岸边', '结冰的湖面和湖边覆雪的树，清晨柔和的光线。'),
        ),
        'avoid': '雪地过曝、画面发灰、脸部偏青',
    },
    {
        'id': 'european_church',
        'name': '欧式教堂',
        'description': '拱廊、彩窗、石柱、庄重浪漫',
        'costume': '新娘：优雅的低盘发，戴一顶小巧的钻石发冠；端庄的妆容，柔和的玫瑰色唇妆；珍珠耳坠，一条超长的蕾丝边教堂头纱；长拖尾的蕾丝婚纱，长袖、V 领，大面积手工蕾丝，裙摆宽大并带数米长的拖尾。新郎：整齐的侧分短发；黑色燕尾服，白色马甲和白衬衫，白色领结。',
        'look': '庄重浪漫的欧式古典氛围，石材的暖灰色调，高大空间里柔和的自然光。',
        'variants': (
            ('教堂通道', '教堂中殿的长通道和两侧的木长椅，高窗的天光洒在通道上。'),
            ('彩色玻璃窗', '彩色玻璃窗前，透过彩窗的柔和光线在地面留下淡淡的彩色光斑。'),
            ('石柱拱廊', '修道院的石柱拱廊，一侧是庭院，阳光在地面投出拱形的影子。'),
            ('古堡石阶', '古堡外宽大的石阶和石雕栏杆，午后的暖阳。'),
        ),
        'avoid': '彩色光斑落在脸上、脸部偏色、画面过暗',
    },
    {
        'id': 'black_white',
        'name': '黑白经典',
        'description': '黑白影像、光影与情绪',
        'costume': '新娘：光洁的低盘发；干净的妆容，深色唇妆在黑白画面里形成对比；简洁的珍珠耳钉，一条长头纱；简约的缎面婚纱，一字肩，没有多余装饰，裙摆垂顺及地。新郎：保持参考照片中的发型轮廓和发际线，打理整齐；黑色西装，白衬衫，黑色领结。',
        'look': '黑白影像，丰富的灰阶层次和细腻的颗粒，靠光影、线条和情绪打动人，画面中没有任何彩色。',
        'variants': (
            ('影棚侧光', '简洁的纯色影棚，侧光塑造出清晰的明暗。'),
            ('老屋窗边', '老房子的窗边，自然窗光从一侧照来，背景是斑驳的墙面和窗框。'),
            ('老城街头', '老城区的街道，阳光在地面投出建筑的影子。'),
            ('旋转楼梯', '一段旋转楼梯，光线从高处的窗户照下来，栏杆形成流畅的线条。'),
        ),
        'avoid': '彩色、偏色、灰蒙蒙缺乏对比、大面积死黑',
    },
    {
        'id': 'daily_life',
        'name': '生活日常',
        'description': '咖啡馆、书店、家中，自然纪实',
        'costume': '新娘：随意挽起的低发髻；淡淡的素颜妆；小巧的耳钉，一条短款头纱；简约轻便的短款婚纱，方领、短袖，裙长及膝，面料是柔软的缎面。新郎：自然的短发；白衬衫，袖口挽起，深色西裤，不打领带。',
        'look': '自然松弛的生活纪实感，温暖柔和的室内光和真实的生活细节，像被朋友抓拍到的瞬间。',
        'variants': (
            ('窗边咖啡馆', '咖啡馆靠窗的座位，桌上放着两杯咖啡，窗外的日光照进来。'),
            ('书店书架', '两排高高的书架之间，暖黄的灯光和天窗透下的柔光。'),
            ('家中厨房', '明亮的家居厨房，台面上摆着水果和花，窗光照进来。'),
            ('客厅沙发', '客厅里的布艺沙发和地毯，午后的窗光照进来。'),
        ),
        'avoid': '杂乱背景、手部或餐具变形、画面偏黄',
    },
)
for _style in _STYLE_DATA:
    # Image prompts name the costume and visual language; the concrete variant is written into each shot.
    # One look per set: without it every shot invents a different gown, veil and hairstyle.
    _style['scene_prompt'] = '服装与风格：' + _style['costume'] + SAME_LOOK + _style['look']
STYLES = tuple(_STYLE_DATA)
STYLE_BY_ID = {style['id']: style for style in STYLES}

_POSE_DATA = (
    ('pose_01', '双人牵手全身照', '动作瞬间：两人手牵手并肩站立，朝镜头方向缓步走近，新娘微微靠向新郎，两人看向镜头，笑容自然，全身入画。'),
    ('pose_02', '双人自然对视', '动作瞬间：两人面对面靠近站立，新郎轻握新娘的双手，两人相视而笑，脸部呈四分之三侧脸朝向镜头，面部清晰，半身构图。'),
    ('pose_03', '新郎从背后拥抱新娘', '动作瞬间：新郎从身后环抱新娘的腰，下巴轻靠在新娘肩侧但不遮挡她的脸，新娘双手搭在新郎手臂上，两人一起看向镜头，七分身构图。'),
    ('pose_04', '双人并肩坐姿', '动作瞬间：两人并肩坐在场景中合适的位置，新娘的头轻靠在新郎肩上，两人看向镜头，姿态放松，七分身构图。'),
    ('pose_05', '双人半身微笑', '动作瞬间：两人肩并肩靠近，新郎的手轻搭在新娘肩上，两人正面看向镜头，露出自然的微笑，半身构图，面部占画面较大比例。'),
    ('pose_06', '亲吻额头', '动作瞬间：新郎轻吻新娘的额头，新娘闭眼微笑，面部正对镜头且不被遮挡，新郎的侧脸轮廓清晰，半身构图。'),
    ('pose_07', '双人自然行走', '动作瞬间：两人手牵手朝镜头方向漫步，新娘一手轻提裙摆，边走边笑着看向镜头，衣摆自然飘动，全身入画。'),
    ('pose_08', '浪漫近景特写', '动作瞬间：两人脸颊靠近、额头几乎相贴，同时看向镜头，表情温柔，近景构图，两人面部清晰完整。'),
    ('pose_09', '双人背靠背', '动作瞬间：两人背靠背站立，头部微微转向镜头，嘴角带笑，面部清晰可见，七分身构图。'),
    ('pose_10', '新娘手持捧花', '动作瞬间：新娘双手将捧花握在胸前，捧花低于下巴、不遮挡脸部，新郎站在她身侧微微低头看着她，新娘看向镜头，七分身构图。'),
    ('pose_11', '双人侧身拥抱', '动作瞬间：两人侧身相拥，新娘脸颊靠近新郎胸口，两人的脸转向镜头约四分之三侧脸，表情幸福，面部不被手臂或头发遮挡，半身构图。'),
    ('pose_12', '双人远景构图', '动作瞬间：两人手牵手站在画面中景，环境占画面更大比例，两人朝向镜头，面部仍清晰可辨。'),
)
POSES = tuple(
    {'id': pose_id, 'name': name, 'prompt_template': prompt_template}
    for pose_id, name, prompt_template in _POSE_DATA
)
POSE_BY_ID = {pose['id']: pose for pose in POSES}

PREVIEW_MOMENT = '镜头：两人并肩站立，新娘挽着新郎的手臂，两人看向镜头，表情自然放松，中景构图，两人面部清晰。'
GENERAL_AVOID = '网红脸、整容脸、幼态脸、五官欧美化、过度磨皮的塑料皮肤、面部被遮挡、多余的人物、手部或手指畸形、人物像抠图贴在背景上、影楼廉价感、文字和水印'
QUALITY_PARAGRAPH = (
    '画面质感：真实相机拍摄的婚纱摄影作品，电影感，极致细节，高级质感。人物与环境处在同一光源下，受光方向、色温和明暗一致，'
    '人物脚下或身后有自然的接触阴影；两人面部曝光充足、清晰；表情自然、真实，符合这个瞬间的情绪；'
    '焦外自然虚化，色彩处理统一，皮肤保留真实质感。'
)
# Retired faceless detail shots (V3.4–V3.9). Old works may still carry it: they have no faces to boost or swap.
DETAIL_SHOT = 'detail'
SHOT_LABEL = '镜头：'
# V2 snapshots label the same paragraph this way; still read when rebuilding old works.
_LEGACY_SHOT_LABEL = '动作瞬间：'

_IDENTITY_PARAGRAPH = (
    '图片角色：第一张图是新娘的身份参考，第二张图是新郎的身份参考。'
    '严格保持两人的长相：脸型、五官比例、眼型、鼻型、唇形、肤色、发际线和年龄感都与第一、二张参考照片一致；'
    '不要美颜、瘦脸、磨皮或改变五官。允许改变的只有发型造型、妆容、服装、姿态、场景和光线。'
    '风格要求与长相冲突时，以长相为准。'
)
_PREVIEW_REFERENCE_INSERTION = '第三张图是风格参考，只参考它的场景、服装、光线和色调，不参考其中的人脸、长相和构图。'


def _aspect_paragraph(aspect_ratio: str) -> str:
    if not isinstance(aspect_ratio, str):
        raise ValueError('aspect_ratio must be a width:height string such as "3:4"')
    match = re.fullmatch(r'([1-9]\d*):([1-9]\d*)', aspect_ratio)
    if match is None:
        raise ValueError('aspect_ratio must be a width:height string such as "3:4"')
    width, height = (int(value) for value in match.groups())
    if width == height:
        return '方构图，1:1 比例的写实婚纱照。'
    if height > width:
        return f'竖构图，{aspect_ratio} 比例的写实婚纱照。'
    return f'横构图，{aspect_ratio} 比例的写实婚纱照。'


def _identity_paragraph(with_preview: bool) -> str:
    if not with_preview:
        return _IDENTITY_PARAGRAPH
    marker = '第二张图是新郎的身份参考。'
    return _IDENTITY_PARAGRAPH.replace(marker, marker + _PREVIEW_REFERENCE_INSERTION, 1)


def _style_paragraph(style: dict, *, with_default_scene: bool) -> str:
    if not with_default_scene:
        return style['scene_prompt']
    return style['scene_prompt'] + '场景：' + style['variants'][0][1]


def _compose_prompt(style_id: str, action: str, aspect_ratio: str, *, with_preview: bool = False,
                    with_default_scene: bool = True) -> str:
    style = STYLE_BY_ID[style_id]
    avoid = f"避免：{GENERAL_AVOID}、{style['avoid']}。"
    return '\n\n'.join((
        _aspect_paragraph(aspect_ratio),
        _identity_paragraph(with_preview),
        _style_paragraph(style, with_default_scene=with_default_scene),
        action,
        QUALITY_PARAGRAPH,
        avoid,
    ))


def compose_preview_prompt(style_id: str, aspect_ratio: str) -> str:
    return _compose_prompt(style_id, PREVIEW_MOMENT, aspect_ratio)


def compose_photo_prompt(style_id: str, pose_id: str, aspect_ratio: str, *, with_preview: bool = False) -> str:
    return _compose_prompt(
        style_id,
        POSE_BY_ID[pose_id]['prompt_template'],
        aspect_ratio,
        with_preview=with_preview,
    )


def director_scene_text(style_id: str, *, single_variant: bool) -> str:
    """Scene block for the director: every variant, or only the preview's variant (docs/prompts.md 7.2)."""
    style = STYLE_BY_ID[style_id]
    lines = ['视觉语言（摄影团队已确定）：' + style['look']]
    if single_variant:
        lines.append('场景（所有镜头都在这个场景里）：' + style['variants'][0][1])
    else:
        lines.append('场景变体（每个镜头选其中一个，并在镜头里写清背景和光影；整组尽量用到不同的变体）：')
        lines.extend(f'{index}. {name}：{text}' for index, (name, text) in enumerate(style['variants'], 1))
    return '\n'.join(lines)


def compose_moment_prompt(style_id: str, moment_text: str, aspect_ratio: str, *, with_preview: bool = False) -> str:
    if not isinstance(moment_text, str) or not moment_text.endswith('。'):
        raise ValueError('moment text must end with exactly one Chinese full stop')
    return _compose_prompt(style_id, SHOT_LABEL + moment_text, aspect_ratio, with_preview=with_preview,
                           with_default_scene=False)


def shot_text_from_prompt(prompt: str) -> str:
    """Return the shot paragraph of a stored prompt without its V2 or V3 label."""
    paragraph = prompt.split('\n\n')[3]
    for label in (SHOT_LABEL, _LEGACY_SHOT_LABEL):
        if paragraph.startswith(label):
            return paragraph.removeprefix(label)
    return paragraph


def _apply_identity_boost(prompt: str) -> str:
    """Apply the four identity-boost changes from docs/prompts.md section 6."""
    paragraphs = prompt.split('\n\n')
    paragraphs[1] += '本次是加强长相的重新生成：两人的长相比风格和动作更重要，宁可画面朴素，也要像本人。'
    paragraphs[2] += '光线均匀柔和，降低风格化。'
    paragraphs[3] = paragraphs[3][:-1] + '，两人面部正对或接近正对镜头，面部完整、清晰、占画面较大比例。'
    paragraphs[-1] = paragraphs[-1][:-1] + '、侧脸、低头、头发遮挡眼睛。'
    return '\n\n'.join(paragraphs)


def compose_identity_boost_prompt(style_id: str, pose_id: str | None, aspect_ratio: str,
                                  *, with_preview: bool = False) -> str:
    prompt = (compose_preview_prompt(style_id, aspect_ratio) if pose_id is None else
              compose_photo_prompt(style_id, pose_id, aspect_ratio, with_preview=with_preview))
    return _apply_identity_boost(prompt)


def compose_identity_boost_moment_prompt(style_id: str, moment_text: str, aspect_ratio: str,
                                         *, with_preview: bool = False) -> str:
    return _apply_identity_boost(compose_moment_prompt(style_id, moment_text, aspect_ratio, with_preview=with_preview))
