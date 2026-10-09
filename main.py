# Sam Ng redesigned Kivy app. Save as main.py in Pydroid 3.
# Optional: profile.png and banner.jpg in the same folder.
import os
from kivy.app import App
from kivy.metrics import dp
from kivy.graphics import Color, RoundedRectangle, Ellipse, Line
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.image import Image
from kivy.uix.video import Video
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.core.window import Window

BG=(.018,.018,.028,1); CYAN=(.05,.88,.84,1); PINK=(1,.12,.62,1); WHITE=(.96,.97,1,1); MUTED=(.52,.54,.61,1); PANEL=(.055,.055,.085,.96)
Window.clearcolor=BG

class SoftButton(Button):
    def __init__(self, fill=(0,0,0,0), outline=None, radius=20, **kw):
        super().__init__(**kw); self.background_normal=''; self.background_down=''; self.background_color=(0,0,0,0); self.fill=fill; self.outline=outline; self.radius=radius
        self.bind(pos=self.draw,size=self.draw,state=self.draw); self.draw()
    def draw(self,*a):
        self.canvas.before.clear()
        with self.canvas.before:
            Color(*(CYAN[:3]+(.17,)) if self.state=='down' else self.fill)
            RoundedRectangle(pos=self.pos,size=self.size,radius=[dp(self.radius)]*4)
            if self.outline:
                Color(*self.outline); Line(rounded_rectangle=(self.x,self.y,self.width,self.height,dp(self.radius)),width=1.1)

class FloatingTab(Button):
    def __init__(self,**kw):
        super().__init__(**kw); self.background_normal=''; self.background_down=''; self.background_color=(0,0,0,0); self.bold=True; self.font_size='14sp'; self.color=MUTED; self.bind(state=self.tint)
    def tint(self,*a): self.color=CYAN if self.state=='down' else MUTED

class Avatar(FloatLayout):
    def __init__(self,size=dp(48),source='profile.png',**kw):
        super().__init__(size_hint=(None,None),size=(size,size),**kw)
        with self.canvas.before:
            Color(*CYAN); self.outer=Ellipse(pos=self.pos,size=self.size)
            Color(*BG); self.inner=Ellipse(pos=self.pos,size=self.size)
        self.bind(pos=self.redraw,size=self.redraw)
        if os.path.exists(source): self.add_widget(Image(source=source,allow_stretch=True,keep_ratio=False))
        else: self.add_widget(Label(text='S',color=WHITE,bold=True,font_size='22sp'))
    def redraw(self,*a):
        self.outer.pos=self.pos; self.outer.size=self.size; self.inner.pos=(self.x+dp(2),self.y+dp(2)); self.inner.size=(self.width-dp(4),self.height-dp(4))

class MediaCard(BoxLayout):
    def __init__(self,path,**kw):
        super().__init__(orientation='vertical',size_hint_y=None,height=dp(300),padding=dp(10),spacing=dp(8),**kw)
        with self.canvas.before: Color(*PANEL); self.bg=RoundedRectangle(pos=self.pos,size=self.size,radius=[dp(18)]*4)
        self.bind(pos=self.redraw,size=self.redraw)
        self.add_widget(Label(text='SAM NG  •  JUST NOW',color=CYAN,size_hint_y=None,height=dp(24),halign='left'))
        ext=os.path.splitext(path)[1].lower()
        media=Video(source=path,state='stop',options={'eos':'stop'},size_hint_y=1) if ext in ('.mp4','.mov','.m4v','.avi','.mkv','.webm') else Image(source=path,allow_stretch=True,keep_ratio=True,size_hint_y=1)
        self.add_widget(media); self.add_widget(Label(text=os.path.basename(path),color=MUTED,size_hint_y=None,height=dp(22),halign='left'))
    def redraw(self,*a): self.bg.pos=self.pos; self.bg.size=self.size

class HomeScreen(Screen):
    def __init__(self,**kw):
        super().__init__(**kw); self.posts=[]
        root=BoxLayout(orientation='vertical',spacing=dp(6),padding=[dp(12),dp(8),dp(12),dp(6)]); self.add_widget(root)
        top=BoxLayout(size_hint_y=None,height=dp(48),spacing=dp(4)); self.tabs={}
        for name in ('STEM','Following','For You'):
            b=FloatingTab(text=name,state='down' if name=='For You' else 'normal'); b.bind(on_release=lambda inst,n=name:self.select_tab(n)); top.add_widget(b); self.tabs[name]=b
        search=SoftButton(text='⌕',font_size='30sp',size_hint=(None,1),width=dp(48),outline=(CYAN[0],CYAN[1],CYAN[2],.35),radius=24); search.bind(on_release=lambda *_:self.toggle_search()); top.add_widget(search); root.add_widget(top)
        root.add_widget(Label(text='[b]SAM NG[/b] [color=05E0D5]DVSG SG[/color]',markup=True,color=WHITE,font_size='16sp',size_hint_y=None,height=dp(34)))
        self.searchbox=TextInput(hint_text='Search Sam Ng…',multiline=False,size_hint_y=None,height=0,opacity=0,background_normal='',background_active='',background_color=(.08,.08,.12,1),foreground_color=WHITE,hint_text_color=MUTED,cursor_color=CYAN,padding=[dp(12),dp(10)]); root.add_widget(self.searchbox)
        self.feed=GridLayout(cols=1,spacing=dp(10),size_hint_y=None,padding=[0,dp(6),0,dp(8)]); self.feed.bind(minimum_height=self.feed.setter('height'))
        scroll=ScrollView(do_scroll_x=False,bar_width=dp(2)); scroll.add_widget(self.feed); root.add_widget(scroll); self.scroll=scroll
        self.empty=BoxLayout(orientation='vertical',spacing=dp(12),size_hint_y=None,height=dp(240),padding=[dp(4),dp(20),dp(4),dp(8)])
        self.empty.add_widget(Label(text='[b]S ís fôr yõu.[/b]',markup=True,color=WHITE,font_size='24sp',size_hint_y=None,height=dp(48)))
        self.empty.add_widget(Label(text='Your feed starts here',color=(.55,.56,.62,1),font_size='18sp'))
        self.empty.add_widget(Label(text='Tap Create to add a photo or video.',color=MUTED,font_size='14sp'))
        create=SoftButton(text='+  Create a post',size_hint_y=None,height=dp(50),fill=(.07,.07,.11,.95),outline=(CYAN[0],CYAN[1],CYAN[2],.22),color=CYAN,font_size='17sp',radius=4); create.bind(on_release=lambda *_:self.pick_media()); self.empty.add_widget(create); self.feed.add_widget(self.empty)
        nav=BoxLayout(size_hint_y=None,height=dp(62),spacing=dp(5))
        for symbol,label in [('⌂','Home'),('','Friends'),('＋','Create'),('▱','Inbox'),('◉','Profile')]:
            if label=='Friends': b=SoftButton(text='Friends',font_size='12sp',fill=(.06,.07,.10,.92),outline=(CYAN[0],CYAN[1],CYAN[2],.42),radius=28)
            elif label=='Profile': b=SoftButton(text='◉\nProfile',font_size='11sp',radius=24)
            else: b=SoftButton(text=f'{symbol}\n{label}',font_size='11sp',radius=22)
            b.bind(on_release=lambda inst,d=label:self.navigate(d)); nav.add_widget(b)
        root.add_widget(nav)
    def select_tab(self,name):
        for n,b in self.tabs.items(): b.state='down' if n==name else 'normal'
    def toggle_search(self):
        self.searchbox.height=dp(42) if self.searchbox.height==0 else 0; self.searchbox.opacity=1 if self.searchbox.height else 0; self.searchbox.focus=bool(self.searchbox.height)
    def navigate(self,d):
        if d=='Create': self.pick_media()
        elif d=='Profile': self.manager.current='profile'
        elif d in ('Friends','Inbox'):
            p=Popup(title=d,content=Label(text=f'{d} will appear here.',color=WHITE),size_hint=(.8,.25),background_color=BG,separator_color=CYAN); p.open()
        else: self.manager.current='home'
    def pick_media(self):
        try:
            from plyer import filechooser
            filechooser.open_file(on_selection=self.file_selected,filters=[['Media','*.png','*.jpg','*.jpeg','*.webp','*.mp4','*.mov','*.m4v','*.avi','*.mkv','*.webm']]); return
        except Exception: pass
        chooser=FileChooserListView(path='/storage/emulated/0',filters=['*.png','*.jpg','*.jpeg','*.webp','*.mp4','*.mov','*.m4v','*.avi','*.mkv','*.webm'])
        box=BoxLayout(orientation='vertical',spacing=dp(8),padding=dp(8)); box.add_widget(chooser); row=BoxLayout(size_hint_y=None,height=dp(44),spacing=dp(8))
        pop=Popup(title='Choose a photo or video',content=box,size_hint=(.95,.9),background_color=BG,separator_color=CYAN)
        cancel=SoftButton(text='Cancel',fill=(.1,.1,.14,1)); choose=SoftButton(text='Use selected file',fill=(.03,.38,.37,1)); row.add_widget(cancel); row.add_widget(choose); box.add_widget(row)
        cancel.bind(on_release=pop.dismiss); choose.bind(on_release=lambda *_:(self.file_selected(chooser.selection),pop.dismiss())); pop.open()
    def file_selected(self,selection):
        if not selection:return
        path=selection[0] if isinstance(selection,(list,tuple)) else selection
        if not path or not os.path.isfile(path):return
        if self.empty.parent is self.feed:self.feed.remove_widget(self.empty)
        self.posts.insert(0,path); self.feed.add_widget(MediaCard(path),index=0); self.scroll.scroll_y=1

class ProfileScreen(Screen):
    def __init__(self,**kw):
        super().__init__(**kw); root=BoxLayout(orientation='vertical',padding=dp(14),spacing=dp(12)); self.add_widget(root)
        header=BoxLayout(size_hint_y=None,height=dp(42)); back=SoftButton(text='‹',font_size='34sp',size_hint_x=None,width=dp(48),radius=24); back.bind(on_release=lambda *_:setattr(self.manager,'current','home')); header.add_widget(back); header.add_widget(Label(text='[b]Profile[/b]',markup=True,color=WHITE,font_size='20sp',halign='left')); root.add_widget(header)
        banner=FloatLayout(size_hint_y=None,height=dp(150))
        with banner.canvas.before: Color(.07,.08,.13,1); bg=RoundedRectangle(pos=banner.pos,size=banner.size,radius=[dp(18)]*4)
        banner.bind(pos=lambda w,*a:setattr(bg,'pos',w.pos),size=lambda w,*a:setattr(bg,'size',w.size))
        if os.path.exists('banner.jpg'): banner.add_widget(Image(source='banner.jpg',allow_stretch=True,keep_ratio=False))
        else: banner.add_widget(Label(text='[color=05E0D5]SAM NG[/color]  •  DVSG SG',markup=True,color=WHITE,font_size='18sp'))
        root.add_widget(banner)
        identity=BoxLayout(size_hint_y=None,height=dp(90),spacing=dp(14)); identity.add_widget(Avatar(size=dp(76)))
        details=BoxLayout(orientation='vertical',spacing=dp(4)); details.add_widget(Label(text='[b]Sam Ng[/b]',markup=True,color=WHITE,font_size='20sp',halign='left')); details.add_widget(Label(text='@samng  •  Creator',color=MUTED,font_size='13sp',halign='left')); identity.add_widget(details); root.add_widget(identity)
        root.add_widget(Label(text='Your profile, posts and creator details.',color=MUTED,font_size='14sp',size_hint_y=None,height=dp(36)))
        edit=SoftButton(text='Edit profile',size_hint_y=None,height=dp(48),fill=(.06,.07,.10,1),outline=(CYAN[0],CYAN[1],CYAN[2],.3),radius=24); edit.bind(on_release=lambda *_:Popup(title='Edit profile',content=Label(text='Profile editing can be connected next.',color=WHITE),size_hint=(.8,.25),background_color=BG,separator_color=CYAN).open()); root.add_widget(edit)
        home=SoftButton(text='Back to Home',size_hint_y=None,height=dp(48),fill=(.03,.35,.34,1),radius=24); home.bind(on_release=lambda *_:setattr(self.manager,'current','home')); root.add_widget(home)
        root.add_widget(Label(text='Place profile.png and banner.jpg beside main.py to use your own images.',color=MUTED,font_size='11sp'))

class SamNgApp(App):
    def build(self):
        sm=ScreenManager(transition=FadeTransition(duration=.18)); sm.add_widget(HomeScreen(name='home')); sm.add_widget(ProfileScreen(name='profile')); return sm
if __name__=='__main__': SamNgApp().run()
