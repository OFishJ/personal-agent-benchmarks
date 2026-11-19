"""Whitelisted Qt GUI event bridge and honest before/after recorder.

No FreeCAD geometry APIs, eval, shell commands, OS input injection or desktop switching.
The only FreeCAD API read is its version and main-window reference.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
from PySide6 import QtCore,QtGui,QtWidgets
try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except ImportError:
    QOpenGLWidget=()

Qt=QtCore.Qt
MODULE_SOURCE_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
EXPECTED_SOURCE_SHA256=os.environ.get('CODEX_CAD_EXPECTED_BRIDGE_SHA256')
if EXPECTED_SOURCE_SHA256 and MODULE_SOURCE_SHA256!=EXPECTED_SOURCE_SHA256:
    raise RuntimeError('Bridge source changed after launch preparation; refusing startup')
user32=ctypes.windll.user32
user32.GetThreadDesktop.argtypes=[ctypes.c_ulong]
user32.GetThreadDesktop.restype=ctypes.c_void_p
user32.OpenInputDesktop.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong]
user32.OpenInputDesktop.restype=ctypes.c_void_p
user32.CloseDesktop.argtypes=[ctypes.c_void_p]
user32.GetUserObjectInformationW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong)]

def desktop_name(handle):
    buffer=ctypes.create_unicode_buffer(256);needed=ctypes.c_ulong()
    if not user32.GetUserObjectInformationW(handle,2,buffer,ctypes.sizeof(buffer),ctypes.byref(needed)):
        raise RuntimeError('Cannot verify desktop identity')
    return buffer.value

def isolation():
    current=desktop_name(user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId()))
    handle=user32.OpenInputDesktop(0,False,1)
    if not handle:raise RuntimeError('Cannot read input desktop')
    try:active=desktop_name(handle)
    finally:user32.CloseDesktop(handle)
    return {'process_desktop':current,'input_desktop':active,'isolated':current!=active}

def atomic_json(path,data):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,path)

class Bridge(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window=window;self.app=QtWidgets.QApplication.instance()
        self.run=Path(os.environ['CODEX_CAD_RUN']).resolve()
        for name in ['requests','responses','observations','raw_events']:(self.run/name).mkdir(exist_ok=True)
        self.busy=False;self.current_id=None;self.raw=[];self.session=None;self.counter=0
        self.quality_issues=[];self.dispatches=[];self.dispatch_stack=[];self.record_state='idle'
        self.disabled_effects=[]
        for name in ('UI_AnimateMenu','UI_FadeMenu','UI_AnimateCombo','UI_AnimateTooltip','UI_FadeTooltip','UI_AnimateToolBox'):
            effect=getattr(Qt.UIEffect,name,None)
            if effect is not None:
                self.app.setEffectEnabled(effect,False);self.disabled_effects.append(name)
        (self.run/'dispatch_journal').mkdir(exist_ok=True)
        self.last_mods=Qt.KeyboardModifier.NoModifier
        self.app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
        self.app.installEventFilter(self)
        self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(50)
        atomic_json(self.run/'ready.json',self.info())

    def info(self):
        version=None
        try:
            import FreeCAD
            version=FreeCAD.Version()[:3]
        except ImportError:pass
        expected=os.environ.get('CODEX_CAD_EXPECTED_VERSION')
        if expected and (not version or '.'.join(version)!=expected):
            raise RuntimeError('FreeCAD version mismatch: expected '+expected+', got '+str(version))
        return {'ready':True,'pid':os.getpid(),'qt_version':QtCore.qVersion(),'freecad_version':version,
                'title':self.window.windowTitle(),'window_modified':self.window.isWindowModified(),
                'width':self.window.width(),'height':self.window.height(),
                'source':'Qt events on a separate local Windows desktop; not physical input hooks',
                'isolation':isolation(),'gui_bridge_sha256':MODULE_SOURCE_SHA256,'disabled_cosmetic_effects':self.disabled_effects}

    def eventFilter(self,obj,event):
        tracked={QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonRelease,
                 QtCore.QEvent.Type.MouseButtonDblClick,QtCore.QEvent.Type.MouseMove,
                 QtCore.QEvent.Type.KeyPress,QtCore.QEvent.Type.KeyRelease,QtCore.QEvent.Type.Wheel,
                 QtCore.QEvent.Type.ShortcutOverride,QtCore.QEvent.Type.Shortcut}
        if self.current_id and event.type() in tracked:
            item={'monotonic':time.monotonic(),'type':event.type().name,
                  'widget_class':obj.metaObject().className(),'object_name':obj.objectName()}
            if hasattr(event,'text'):item['text']=event.text()
            if hasattr(event,'key'):
                value=event.key()
                item['key']=value.toString(QtGui.QKeySequence.SequenceFormat.PortableText) if isinstance(value,QtGui.QKeySequence) else value
            if hasattr(event,'position'):item['local_position']=[event.position().x(),event.position().y()]
            if hasattr(event,'button'):item['button']=int(event.button().value)
            self.raw.append(item)
        return False

    def surfaces(self):
        # Active modal/pop-up must be considered above their containing main window.
        all_windows=[self.window]+[w for w in self.app.topLevelWidgets() if w is not self.window and w.isVisible()]
        for special in [self.app.activeModalWidget(),self.app.activePopupWidget()]:
            if special in all_windows:all_windows.remove(special);all_windows.append(special)
        return all_windows

    def snapshot(self,path):
        if self.session and (self.window.width(),self.window.height())!=self.record_size:
            raise RuntimeError('GUI canvas dimensions changed during recording; preserve and start a new attempt')
        pixmap=self.window.grab()
        if pixmap.isNull():raise RuntimeError('Main Qt window returned a null pixmap')
        debug_dir=self.run/'debug_frames';debug_dir.mkdir(exist_ok=True)
        raw_path=debug_dir/(path.stem+'_'+str(time.time_ns())+'_qt_raw.png')
        pixmap.save(str(raw_path),'PNG')
        image=pixmap.toImage().scaled(self.window.width(),self.window.height())
        image.setDevicePixelRatio(1)
        painter=QtGui.QPainter(image)
        gl_info=[];capture_warnings=[]
        surfaces=self.surfaces()
        try:
            for surface in surfaces:
                origin=surface.mapToGlobal(QtCore.QPoint())-self.window.mapToGlobal(QtCore.QPoint())
                if not QtCore.QRect(origin,surface.size()).intersects(self.window.rect()):continue
                if surface is not self.window:
                    if surface.metaObject().className() in ('QRollEffect','QAlphaWidget'):
                        capture_warnings.append({'class':surface.metaObject().className(),'reason':'decorative_animation_shell_omitted'})
                        continue
                    layer_pixmap=surface.grab()
                    if layer_pixmap.isNull():raise RuntimeError('Visible transient window returned a null pixmap: '+surface.metaObject().className())
                    layer=layer_pixmap.toImage().scaled(surface.width(),surface.height())
                    layer.setDevicePixelRatio(1);painter.drawImage(origin,layer)
                if QOpenGLWidget:
                    for widget in surface.findChildren(QOpenGLWidget):
                        if not widget.isVisible():continue
                        offset=widget.mapToGlobal(QtCore.QPoint())-self.window.mapToGlobal(QtCore.QPoint())
                        if not QtCore.QRect(offset,widget.size()).intersects(self.window.rect()):continue
                        frame=widget.grabFramebuffer()
                        if frame.isNull():raise RuntimeError('OpenGL framebuffer is null')
                        frame=frame.scaled(widget.width(),widget.height());frame.setDevicePixelRatio(1)
                        # QWidget.grab() already contains the Qt-composited GL
                        # texture and higher QWidget overlays. Repainting the
                        # bare framebuffer here would erase Tasks/overlay panes.
                        gl_info.append({'class':widget.metaObject().className(),'valid':widget.isValid(),
                                        'width':widget.width(),'height':widget.height(),
                                        'x':offset.x(),'y':offset.y()})
        finally:painter.end()
        if not image.save(str(path),'PNG'):raise RuntimeError('PNG save failed')
        return {'image':str(path),'width':image.width(),'height':image.height(),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'time':time.time(),'qt_raw_image':str(raw_path),
                'capture_warnings':capture_warnings,'capture_complete':not capture_warnings,
                'gl_widgets':gl_info,'surfaces':[w.windowTitle() for w in surfaces]}

    def tree(self):
        items=[]
        for surface in self.surfaces():
            for widget in [surface]+surface.findChildren(QtWidgets.QWidget):
                if not widget.isVisible():continue
                point=widget.mapToGlobal(QtCore.QPoint())-self.window.mapToGlobal(QtCore.QPoint())
                text=''
                for attr in ['text','title','currentText','toPlainText']:
                    try:
                        value=getattr(widget,attr)()
                        if isinstance(value,str) and value:text=value[:400];break
                    except (AttributeError,TypeError):pass
                selected=''
                try:selected=widget.selectedText()
                except AttributeError:pass
                items.append({'class':widget.metaObject().className(),'name':widget.objectName(),
                              'text':text,'x':point.x(),'y':point.y(),'width':widget.width(),
                              'height':widget.height(),'enabled':widget.isEnabled(),
                              'tooltip':widget.toolTip(),'selected_text':selected,
                              'focus':widget.hasFocus()})
                if len(items)>=500:return items
        return items

    def at(self,x,y):
        if not (0<=x<self.window.width() and 0<=y<self.window.height()):
            raise ValueError('Coordinates outside captured GUI canvas')
        global_point=self.window.mapToGlobal(QtCore.QPoint(x,y))
        for surface in reversed(self.surfaces()):
            point=surface.mapFromGlobal(global_point)
            if surface.rect().contains(point):
                widget=surface.childAt(point) or surface
                return widget,widget.mapFromGlobal(global_point),global_point
        raise ValueError('No visible Qt widget at coordinate')

    def modifiers(self,value):
        result=Qt.KeyboardModifier.NoModifier
        for name in value.lower().split('+'):
            if name in ('ctrl','control'):result|=Qt.KeyboardModifier.ControlModifier
            elif name=='shift':result|=Qt.KeyboardModifier.ShiftModifier
            elif name=='alt':result|=Qt.KeyboardModifier.AltModifier
            elif name:raise ValueError('Unsupported modifier '+name)
        return result

    def send_qt(self,widget,event):
        context=self.dispatch_stack[-1]
        dispatches=context['dispatches']
        item={'index':len(dispatches)+1,'phase':'attempting','time':time.time(),
              'type':event.type().name,'widget_class':widget.metaObject().className(),
              'object_name':widget.objectName()}
        if hasattr(event,'text'):item['text']=event.text()
        if hasattr(event,'key'):
            value=event.key()
            item['key']=value.toString(QtGui.QKeySequence.SequenceFormat.PortableText) if isinstance(value,QtGui.QKeySequence) else value
        dispatches.append(item)
        journal=self.run/'dispatch_journal'/(context['request']['id']+'.json')
        atomic_json(journal,{'request_id':context['request']['id'],'dispatch_attempts':dispatches})
        # Commit the actual attempted input before dispatch. sendEvent can enter
        # a nested modal event loop; timer capture must remain able to finish it.
        if context['session'] is not None and not context['committed']:
            context['session']['ground_truth_actions'].append(self.step(context['request']['method'],context['request'].get('args',{}),context['before']))
            context['session']['final_state_image']=None;context['committed']=True;self.flush()
        try:
            item['send_event_return']=bool(self.app.sendEvent(widget,event))
            item['event_accepted']=bool(event.isAccepted());item['phase']='returned'
        except Exception as exc:
            item['phase']='raised';item['error']=repr(exc);raise
        finally:
            item['completed_at']=time.time()
            atomic_json(journal,{'request_id':context['request']['id'],'dispatch_attempts':dispatches})

    def mouse_event(self,widget,local,global_point,kind,button,buttons,mods):
        event=QtGui.QMouseEvent(kind,QtCore.QPointF(local),QtCore.QPointF(global_point),button,buttons,mods)
        self.send_qt(widget,event)

    def click(self,args,double=False,right=False):
        widget,local,global_point=self.at(int(args['x']),int(args['y']))
        popup=self.app.activePopupWidget()
        if popup is not None and not popup.rect().contains(popup.mapFromGlobal(global_point)):
            # Deliver outside presses to Qt's current popup grabber. Sending to
            # the underlying child directly skips the normal menu-dismiss path.
            widget=popup;local=popup.mapFromGlobal(global_point)
        if widget.focusPolicy()!=Qt.FocusPolicy.NoFocus:widget.setFocus(Qt.FocusReason.MouseFocusReason)
        button=Qt.MouseButton.RightButton if right or args.get('button')=='right' else (
            Qt.MouseButton.MiddleButton if args.get('button')=='middle' else Qt.MouseButton.LeftButton)
        mods=self.modifiers(args.get('modifier',''))
        self.mouse_event(widget,local,global_point,QtCore.QEvent.Type.MouseMove,Qt.MouseButton.NoButton,Qt.MouseButton.NoButton,mods)
        self.mouse_event(widget,local,global_point,QtCore.QEvent.Type.MouseButtonPress,button,button,mods)
        self.mouse_event(widget,local,global_point,QtCore.QEvent.Type.MouseButtonRelease,button,Qt.MouseButton.NoButton,mods)
        if double:
            self.mouse_event(widget,local,global_point,QtCore.QEvent.Type.MouseButtonDblClick,button,button,mods)
            self.mouse_event(widget,local,global_point,QtCore.QEvent.Type.MouseButtonRelease,button,Qt.MouseButton.NoButton,mods)

    def key(self,name,text=None):
        popup=self.app.activePopupWidget()
        modal=self.app.activeModalWidget()
        focus=self.app.focusWidget()
        widget=popup or (focus if modal is None or (focus and modal.isAncestorOf(focus)) else modal) or self.window
        sequence=QtGui.QKeySequence(name)
        if sequence.isEmpty():raise ValueError('Unsupported key '+name)
        combination=sequence[0];key=combination.key().value;mods=combination.keyboardModifiers()
        self.send_qt(widget,QtGui.QKeyEvent(QtCore.QEvent.Type.ShortcutOverride,key,mods,text or ''))
        self.send_qt(widget,QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,key,mods,text or ''))
        self.send_qt(widget,QtGui.QKeyEvent(QtCore.QEvent.Type.KeyRelease,key,mods,text or ''))

    def apply(self,method,args):
        if method in ('click','right_click','double_click'):
            self.click(args,double=method=='double_click',right=method=='right_click')
        elif method in ('type','type_and_enter'):
            text=args['text']
            if any(ord(c)<32 or ord(c)>126 for c in text):raise ValueError('Only printable ASCII key input is currently verified')
            for char in text:self.key('Space' if char==' ' else (char.upper() if char.isalpha() else char),text=char)
            if method=='type_and_enter':self.key('Return')
        elif method=='hotkey':
            name=args['key']
            if any(part.lower() in ('win','windows','meta','super') for part in name.split('+')):
                raise ValueError('OS shortcuts are not allowed')
            if name.lower() in ('ctrl+v','control+v','shift+insert'):
                raise ValueError('Clipboard input is not replayable; use type')
            self.key(name)
        elif method=='drag':
            widget,local,point=self.at(*args['start'])
            end=QtCore.QPoint(*args['end']);end_global=self.window.mapToGlobal(end)
            mods=self.modifiers(args.get('modifier',''))
            buttons={'left':Qt.MouseButton.LeftButton,'middle':Qt.MouseButton.MiddleButton,'right':Qt.MouseButton.RightButton}
            if args.get('button','left') not in buttons:raise ValueError('Unsupported drag button')
            button=buttons[args.get('button','left')]
            self.mouse_event(widget,local,point,QtCore.QEvent.Type.MouseButtonPress,button,button,mods)
            for n in range(1,13):
                p=point+(end_global-point)*n/12
                self.mouse_event(widget,widget.mapFromGlobal(p),p,QtCore.QEvent.Type.MouseMove,Qt.MouseButton.NoButton,button,mods)
            self.mouse_event(widget,widget.mapFromGlobal(end_global),end_global,QtCore.QEvent.Type.MouseButtonRelease,button,Qt.MouseButton.NoButton,mods)
        elif method=='scroll':
            widget,local,point=self.at(int(args['x']),int(args['y']))
            delta=int(args.get('amount',1))*120*(1 if args['direction']=='up' else -1)
            event=QtGui.QWheelEvent(QtCore.QPointF(local),QtCore.QPointF(point),QtCore.QPoint(),QtCore.QPoint(0,delta),
                                   Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
            self.send_qt(widget,event)
        else:raise ValueError('Unsupported GUI action '+method)

    def step(self,method,args,before):
        step={'step':len(self.session['ground_truth_actions'])+1,'image_path':before['image'],
              'human_intent':'TO_FILL','action_type':method}
        if method in ('click','right_click','double_click'):
            step.update(coordinate=[args['x'],args['y']],modifier=args.get('modifier',''))
            if method!='right_click':step['button']=args.get('button','left')
        elif method=='drag':step.update(start_coordinate=args['start'],end_coordinate=args['end'],button=args.get('button','left'),modifier=args.get('modifier',''))
        elif method=='scroll':step.update(coordinate=[args['x'],args['y']],direction=args['direction'],amount=args.get('amount',1))
        elif method in ('type','type_and_enter'):step['text']=args['text']
        elif method=='hotkey':step['key']=args['key']
        return step

    def flush(self):
        if not self.session:return
        self.session['total_steps']=len(self.session['ground_truth_actions'])
        atomic_json(self.record_dir/'trajectory_000.json',self.session)
        atomic_json(self.record_dir/'_meta.json',{'source':self.info(),'task':self.task,
            'framebuffer':[self.window.width(),self.window.height()],
            'coordinate_system':'main Qt client logical pixels; screenshots resampled to same size',
            'physical_hooks':False,'computer_use':False,'os_input_injection':False,
            'quality_issues':self.quality_issues,'record_state':self.record_state,
            'dispatch_evidence':'persistent send_qt attempts and return/accepted flags; separate from event-filter observations; neither proves GUI effect',
            'format_basis':'FreeCAD GUI trajectory with pre-action screenshots and one final screenshot',
            'recorder_sha256':MODULE_SOURCE_SHA256})

    def finish_action(self,context,attempt=0,last_frame=None):
        request=context['request'];before=context['before'];raw_start=context['raw_start'];error=context['error']
        dispatches=context['dispatches'];after=None
        try:
            after=self.snapshot(self.run/'observations'/(request['id']+'_after.png'))
            frame=QtGui.QImage(after['image']).scaled(120,68).convertToFormat(QtGui.QImage.Format.Format_RGB32)
            data=bytes(frame.constBits())
            if last_frame is None and attempt==0:
                QtCore.QTimer.singleShot(200,lambda:self.finish_action(context,1,data));return
            diff=sum(abs(a-b) for a,b in zip(data,last_frame))/max(1,len(data)) if last_frame else None
            if diff is not None and diff>1.0 and attempt<5:
                QtCore.QTimer.singleShot(200,lambda:self.finish_action(context,attempt+1,data));return
            response={'ok':error is None and after['capture_complete'],'before':before,'after':after,
                      'events':self.raw[raw_start:],'dispatch_attempts':dispatches,
                      'stability':{'samples':attempt+1,'mean_pixel_difference':diff,
                                   'verified':diff is not None and diff<=1.0,
                                   'scope':'low-resolution global image heuristic; inspect actual image'},
                      'error':error,'isolation':isolation()}
            if self.session:
                self.session['final_state_image']=after['image']
                if error or not after['capture_complete'] or not response['stability']['verified']:
                    self.quality_issues.append({'step':len(self.session['ground_truth_actions']),
                        'request_id':request['id'],'error':error,'capture_warnings':after['capture_warnings'],
                        'stability':response['stability']})
                self.flush()
        except Exception as exc:
            response={'ok':False,'before':before,'after':after,'error':repr(exc),
                      'events':self.raw[raw_start:],'dispatch_attempts':dispatches,
                      'input_may_have_occurred':bool(dispatches),'traceback':traceback.format_exc()}
            if self.session:
                self.session['final_state_image']=None
                self.quality_issues.append({'step':len(self.session['ground_truth_actions']),
                    'request_id':request['id'],'after_capture_failed':repr(exc),'input_outcome':'unknown; do not resend blindly'})
                self.flush()
        try:
            atomic_json(self.run/'raw_events'/(request['id']+'.json'),{'request':request,'response':response})
        except Exception as exc:
            self.respond(request,{'ok':False,'error':'Evidence persistence failed: '+repr(exc),
                'input_may_have_occurred':bool(dispatches),'before_image':before['image']});return
        self.respond(request,response)

    def respond(self,request,response):
        atomic_json(self.run/'responses'/(request['id']+'.json'),response)
        self.current_id=None;self.busy=False

    def poll(self):
        if self.busy:return
        for path in sorted((self.run/'requests').glob('*.json')):
            if (self.run/'responses'/path.name).exists():continue
            self.busy=True
            QtCore.QTimer.singleShot(0,lambda p=path:self.handle_request(p))
            return

    def handle_request(self,path):
        try:
            request=json.loads(path.read_text(encoding='utf-8'));self.current_id=request['id'];self.dispatches=[]
            if not isolation()['isolated']:raise RuntimeError('Refusing GUI input on the user input desktop')
            method=request['method'];args=request.get('args',{})
            if method=='observe':
                frame=self.snapshot(self.run/'observations'/(request['id']+'.png'))
                self.respond(request,{'ok':True,'frame':frame,'tree':self.tree(),'info':self.info()})
            elif method=='start_record':
                if self.session:raise RuntimeError('A recording is already active')
                if not args.get('name','').replace('_','').replace('-','').isalnum():raise ValueError('Invalid record name')
                self.record_dir=self.run/'records'/args['name'];self.record_dir.mkdir(parents=True,exist_ok=False)
                self.quality_issues=[];self.record_state='active'
                self.record_size=(self.window.width(),self.window.height())
                (self.record_dir/'raw_images').mkdir();self.task=args.get('task','NEUTRAL')
                self.session={'task_id':'FREECAD_RECORD_000','task_category':'FreeCAD GUI Agent Training',
                              'total_steps':0,'final_state_image':None,'ground_truth_actions':[]}
                initial=self.snapshot(self.record_dir/'initial_aux.png');self.flush()
                self.respond(request,{'ok':True,'record_dir':str(self.record_dir),'initial':initial})
            elif method in ('stop_record','stop_record_partial'):
                if not self.session:raise RuntimeError('No recording is active')
                final=None;capture_error=None
                try:
                    final=self.snapshot(self.record_dir/'raw_images'/('step_%03d.png'%len(self.session['ground_truth_actions'])))
                    if not final['capture_complete']:raise RuntimeError('Final frame has capture warnings')
                except Exception as exc:
                    capture_error=repr(exc)
                    if method!='stop_record_partial':raise
                if capture_error:
                    self.record_state='interrupted';self.session['final_state_image']=None
                    self.quality_issues.append({'stop_capture_failed':capture_error,
                        'scope':'Incomplete recording preserved without a fabricated final frame or N+1 claim'})
                else:
                    self.record_state='stopped';self.session['final_state_image']=final['image']
                self.flush()
                response={'ok':True,'complete':capture_error is None,'record_state':self.record_state,
                    'record_dir':str(self.record_dir),'steps':self.session['total_steps'],'final':final,'capture_error':capture_error}
                self.session=None;self.respond(request,response)
            elif method in ('click','right_click','double_click','drag','scroll','type','type_and_enter','hotkey'):
                before_path=(self.record_dir/'raw_images'/('step_%03d.png'%len(self.session['ground_truth_actions']))) if self.session else self.run/'observations'/(request['id']+'_before.png')
                before=self.snapshot(before_path)
                if not before['capture_complete']:raise RuntimeError('Refusing input: before frame contains capture warnings')
                raw_start=len(self.raw)
                context={'request':request,'before':before,'raw_start':raw_start,'error':None,
                         'dispatches':[],'session':self.session,'committed':False}
                atomic_json(self.run/'raw_events'/(request['id']+'.json'),
                    {'request':request,'before':before,'dispatch_attempts':[], 'after_capture':'pending'})
                QtCore.QTimer.singleShot(350,lambda c=context:self.finish_action(c))
                self.dispatch_stack.append(context)
                try:self.apply(method,args)
                except Exception as exc:context['error']=repr(exc)
                finally:self.dispatch_stack.pop()
                if context['session'] is not None and not context['dispatches']:
                    rejected=self.run/'observations'/(request['id']+'_undispatched_before.png')
                    Path(before['image']).replace(rejected);before['image']=str(rejected)
            else:raise ValueError('Method is not whitelisted')
        except Exception as exc:
            self.respond(request if 'request' in locals() else {'id':path.stem},{'ok':False,'error':repr(exc),'traceback':traceback.format_exc()})

"""Appended to a preserved v4 source copy; no runtime imports of test fixtures."""
import shiboken6
try:
    import numpy as np
except ImportError:
    np=None

DELIVERY_TYPE=QtCore.QEvent.Type(QtCore.QEvent.registerEventType())
LEGACY_KEYS={'tab':'tab','delete':'delete','esc':'esc','escape':'esc','enter':'enter','return':'enter'}

def alive(obj):
    return obj is not None and shiboken6.isValid(obj)

def save_png(obj,path):
    tmp=path.with_name(path.name+'.'+str(time.time_ns())+'.tmp')
    if not obj.save(str(tmp),'PNG'):raise RuntimeError('PNG write failed')
    check=QtGui.QImage(str(tmp))
    if check.isNull() or check.size()!=obj.size():raise RuntimeError('PNG decode/dimensions failed')
    os.replace(tmp,path)

class Delivery(QtCore.QEvent):
    def __init__(self,context,item,target,event):
        super().__init__(DELIVERY_TYPE)
        self.context=context;self.item=item;self.target=target;self.input_event=event

class DeliveryRouter(QtCore.QObject):
    def __init__(self,bridge):super().__init__(bridge);self.bridge=bridge
    def event(self,event):
        if event.type()==DELIVERY_TYPE:
            self.bridge.deliver(event);return True
        return super().event(event)

class QueuedBridge(Bridge):
    def __init__(self,window):
        self.contexts={};self.active_deliveries=[];self.key_policy='legacy-strict'
        self.session_closed=False;self.event_serial=0;self.direct_events={};self.seen_requests=set();self.persistence_fault=None;self.stop_quiet_tokens=set()
        super().__init__(window)
        self.router=DeliveryRouter(self)
        for name in ['dispatch_lifecycle','dispatch_observations','completion_updates','unattributed_events','layers']:
            (self.run/name).mkdir(exist_ok=True)
        atomic_json(self.run/'ready.json',self.info())

    def pending(self):
        return [{'request_id':c['request']['id'],'dispatch_id':d['dispatch_id'],'phase':d['phase']}
                for c in self.contexts.values() for d in c['dispatches']
                if d['phase'] in ('queued','entered')]

    def info(self):
        data=super().info()
        data.update(bridge_version='v5-isolated',transport='queued envelopes; per-event sendEvent lifecycle',
                    pending_dispatches=self.pending(),key_policy=self.key_policy,
                    handler_completion_is_not_gui_semantic_completion=True,persistence_fault=self.persistence_fault)
        return data

    def eventFilter(self,obj,event):
        tracked={QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonRelease,
                 QtCore.QEvent.Type.MouseButtonDblClick,QtCore.QEvent.Type.MouseMove,
                 QtCore.QEvent.Type.KeyPress,QtCore.QEvent.Type.KeyRelease,QtCore.QEvent.Type.Wheel,
                 QtCore.QEvent.Type.ShortcutOverride,QtCore.QEvent.Type.Shortcut,QtCore.QEvent.Type.ContextMenu}
        if event.type() not in tracked:return False
        pointer=shiboken6.getCppPointer(event)[0]
        context,item=self.direct_events.get(pointer,(None,None))
        observed_context,observed_item=self.active_deliveries[-1] if self.active_deliveries else (None,None)
        observed={'monotonic':time.monotonic(),'time':time.time(),'type':event.type().name,
                  'origin_request_id':context['request']['id'] if context else None,
                  'origin_dispatch_id':item['dispatch_id'] if item else None,
                  'observed_while_request_id':self.current_id,
                  'attribution':'exact live event pointer' if context else 'uncorrelated; not guessed',
                  'observed_during_delivery_id':observed_item['dispatch_id'] if observed_item else None,
                  'widget_class':obj.metaObject().className() if alive(obj) else '<deleted>',
                  'object_name':obj.objectName() if alive(obj) else ''}
        if hasattr(event,'text'):observed['text']=event.text()
        if hasattr(event,'key'):
            value=event.key()
            observed['key']=value.toString(QtGui.QKeySequence.SequenceFormat.PortableText) if isinstance(value,QtGui.QKeySequence) else value
        if hasattr(event,'position'):observed['local_position']=[event.position().x(),event.position().y()]
        if hasattr(event,'button'):observed['button']=int(event.button().value)
        self.event_serial+=1
        try:
            if context:
                context['events'].append(observed)
                folder=self.run/'dispatch_observations'/context['request']['id'];folder.mkdir(exist_ok=True)
                atomic_json(folder/('%09d.json'%self.event_serial),observed)
            else:atomic_json(self.run/'unattributed_events'/('%09d.json'%self.event_serial),observed)
        except Exception as exc:
            self.persistence_fault='Observation persistence failed: '+repr(exc)
            if context:context['error']=self.persistence_fault
        return False

    def surfaces(self):
        windows=[self.window]
        for widget in self.app.topLevelWidgets():
            if alive(widget) and widget is not self.window and widget.isVisible():windows.append(widget)
        for special in [self.app.activeModalWidget(),self.app.activePopupWidget()]:
            if alive(special) and special in windows:windows.remove(special);windows.append(special)
        return windows

    def at(self,x,y):
        if not (0<=x<self.window.width() and 0<=y<self.window.height()):raise ValueError('Outside GUI canvas')
        point=self.window.mapToGlobal(QtCore.QPoint(x,y))
        for surface in reversed(self.surfaces()):
            if not alive(surface) or surface.metaObject().className() in ('QRollEffect','QAlphaWidget','QTipLabel'):continue
            local=surface.mapFromGlobal(point)
            if surface.rect().contains(local):
                target=surface.childAt(local) or surface
                if not alive(target):raise RuntimeError('Hit-test target deleted before dispatch')
                return target,target.mapFromGlobal(point),point
        raise RuntimeError('No live visible target')

    def keyboard_target(self):
        popup=self.app.activePopupWidget();modal=self.app.activeModalWidget();focus=self.app.focusWidget()
        if alive(popup):
            # QComboBoxPrivateContainer needs its focused list/view child;
            # QMenu normally owns focus itself.
            return focus if alive(focus) and (focus is popup or popup.isAncestorOf(focus)) else popup
        if alive(modal):return focus if alive(focus) and (focus is modal or modal.isAncestorOf(focus)) else modal
        return focus if alive(focus) else self.window

    def lifecycle(self,context,item):
        folder=self.run/'dispatch_lifecycle'/context['request']['id'];folder.mkdir(exist_ok=True)
        atomic_json(folder/('%04d_%s.json'%(item['index'],item['phase'])),dict(item))

    def send_qt(self,widget,event):
        context=self.dispatch_stack[-1]
        item={'index':len(context['dispatches'])+1,'phase':'planned','planned_at':time.time(),
              'type':event.type().name,'route':'keyboard' if isinstance(event,QtGui.QKeyEvent) else 'static'}
        item['dispatch_id']=context['request']['id']+':'+str(item['index'])
        if hasattr(event,'text'):item['text']=event.text()
        if hasattr(event,'key'):item['key']=int(event.key())
        context['dispatches'].append(item);self.lifecycle(context,item)
        context['outbound'].append(Delivery(context,item,widget,event))

    def mouse_event(self,widget,local,global_point,kind,button,buttons,mods):
        context=self.dispatch_stack[-1]
        item={'index':len(context['dispatches'])+1,'phase':'planned','planned_at':time.time(),
              'type':kind.name,'route':'mouse','global_point':[global_point.x(),global_point.y()],
              'button':int(button.value),'buttons':int(buttons.value),'modifiers':int(mods.value)}
        item['dispatch_id']=context['request']['id']+':'+str(item['index'])
        context['dispatches'].append(item);self.lifecycle(context,item)
        context['outbound'].append(Delivery(context,item,None,None))

    def click(self,args,double=False,right=False):
        # Plan without setFocus or stale viewport references. Popup focus is
        # owned by Qt; forcing it caused lifetime hazards in the old adapter.
        _,_,point=self.at(int(args['x']),int(args['y']))
        button=Qt.MouseButton.RightButton if right or args.get('button')=='right' else (
            Qt.MouseButton.MiddleButton if args.get('button')=='middle' else Qt.MouseButton.LeftButton)
        mods=self.modifiers(args.get('modifier',''))
        for kind,b,buttons in [(QtCore.QEvent.Type.MouseMove,Qt.MouseButton.NoButton,Qt.MouseButton.NoButton),
                               (QtCore.QEvent.Type.MouseButtonPress,button,button),
                               (QtCore.QEvent.Type.MouseButtonRelease,button,Qt.MouseButton.NoButton)]:
            self.mouse_event(None,None,point,kind,b,buttons,mods)
        if double:
            self.mouse_event(None,None,point,QtCore.QEvent.Type.MouseButtonDblClick,button,button,mods)
            self.mouse_event(None,None,point,QtCore.QEvent.Type.MouseButtonRelease,button,Qt.MouseButton.NoButton,mods)
        if right:self.queue_context_menu(point)

    def queue_context_menu(self,point):
        context=self.dispatch_stack[-1]
        item={'index':len(context['dispatches'])+1,'phase':'planned','planned_at':time.time(),
              'type':'ContextMenu','route':'context-menu','global_point':[point.x(),point.y()]}
        item['dispatch_id']=context['request']['id']+':'+str(item['index'])
        context['dispatches'].append(item);self.lifecycle(context,item)
        context['outbound'].append(Delivery(context,item,None,None))

    def key(self,name,text=None):
        aliases={'esc':'Escape','enter':'Return','delete':'Delete','tab':'Tab'}
        name=aliases.get(name.lower(),name);sequence=QtGui.QKeySequence(name)
        if sequence.isEmpty() or sequence.count()!=1:raise ValueError('Only one key combination per hotkey request is supported')
        combination=sequence[0];key=combination.key().value;mods=combination.keyboardModifiers()
        for kind in [QtCore.QEvent.Type.ShortcutOverride,QtCore.QEvent.Type.KeyPress,QtCore.QEvent.Type.KeyRelease]:
            self.send_qt(None,QtGui.QKeyEvent(kind,key,mods,text or ''))

    def deliver(self,envelope):
        context,item=envelope.context,envelope.item
        tokens=context.setdefault('pressed_tokens',set())
        token=None
        if item['type'] in ('KeyPress','KeyRelease'):token=('key',item.get('key'))
        elif item['type'] in ('MouseButtonPress','MouseButtonDblClick','MouseButtonRelease'):token=('mouse',item.get('button'))
        release=item['type'] in ('KeyRelease','MouseButtonRelease')
        cleanup=release and token in tokens
        if (context.get('cancel_remaining') or self.persistence_fault) and not cleanup:
            item.update(phase='cancelled_not_dispatched',time=time.time())
            try:self.lifecycle(context,item)
            except Exception as exc:self.persistence_fault=repr(exc)
            return
        if (context.get('cancel_remaining') or self.persistence_fault) and cleanup:item['recovery_cleanup_release']=True
        entered=False
        try:
            if not isolation()['isolated']:raise RuntimeError('Input desktop is no longer isolated')
            target=envelope.target;event=envelope.input_event
            if item['route']=='keyboard':target=self.keyboard_target()
            elif item['route']=='context-menu':
                if alive(self.app.activePopupWidget()):
                    item.update(phase='suppressed_not_dispatched',reason='popup already opened by right mouse sequence');self.lifecycle(context,item);return
                point=QtCore.QPoint(*item['global_point']);target=context.get('press_target')
                if not alive(target):raise RuntimeError('Context menu target deleted; no retry')
                event=QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse,target.mapFromGlobal(point),point)
            elif item['route']=='mouse':
                point=QtCore.QPoint(*item['global_point']);kind=QtCore.QEvent.Type[item['type']]
                grab=QtWidgets.QWidget.mouseGrabber();popup=self.app.activePopupWidget()
                if alive(grab):target=grab
                elif kind in (QtCore.QEvent.Type.MouseButtonRelease,QtCore.QEvent.Type.MouseMove) and alive(context.get('press_target')):target=context['press_target']
                else:
                    logical=point-self.window.mapToGlobal(QtCore.QPoint());target,_,_=self.at(logical.x(),logical.y())
                if alive(popup) and not popup.rect().contains(popup.mapFromGlobal(point)) and kind==QtCore.QEvent.Type.MouseButtonPress:target=popup
                if kind==QtCore.QEvent.Type.MouseButtonPress:context['press_target']=target
                if not alive(target):raise RuntimeError('Mouse target deleted before dispatch')
                local=target.mapFromGlobal(point)
                event=QtGui.QMouseEvent(kind,QtCore.QPointF(local),QtCore.QPointF(point),Qt.MouseButton(item['button']),Qt.MouseButton(item['buttons']),Qt.KeyboardModifier(item['modifiers']))
            if not alive(target) and item.get('recovery_cleanup_release'):
                target=self.window;item['cleanup_target_fallback']='main window; original target deleted'
            if not alive(target):raise RuntimeError('Qt target deleted before dispatch')
            # All fallible preparation/IO precedes the actual target call.
            if context['session'] is not None and (context['session'] is not self.session or self.session_closed):
                raise RuntimeError('Session changed before input; refusing')
            item.update(phase='prepared',prepared_at=time.time(),widget_class=target.metaObject().className(),object_name=target.objectName(),call_attempted=False)
            try:self.lifecycle(context,item)
            except Exception as exc:
                if not item.get('recovery_cleanup_release'):raise
                context['error']='Persistence failure; cleanup release attempted: '+repr(exc)
            pointer=shiboken6.getCppPointer(event)[0]
            self.active_deliveries.append((context,item));self.direct_events[pointer]=(context,item)
            # No IO or event-loop yield between this flag and sendEvent. A
            # timer seeing entered is running inside that call's nested loop.
            if token:
                if release:tokens.discard(token)
                else:tokens.add(token)
            try:
                item.update(phase='entered',entered_at=time.time(),call_attempted=True);entered=True
                item['send_event_return']=bool(self.app.sendEvent(target,event));item['event_accepted']=bool(event.isAccepted())
            finally:
                self.direct_events.pop(pointer,None);self.active_deliveries.pop()
            item.update(phase='returned',completed_at=time.time())
        except Exception as exc:
            item.update(phase='raised' if entered else 'undeliverable',error=repr(exc),completed_at=time.time())
            context['error']=repr(exc);context['cancel_remaining']=True
        finally:
            # Commit only after a call really began, never on pre-send IO error.
            self.commit_context(context)
            try:self.lifecycle(context,item)
            except Exception as exc:self.persistence_fault=repr(exc);context['error']='Lifecycle persistence failed: '+repr(exc)
            if context.get('response_sent'):
                try:
                    atomic_json(self.run/'completion_updates'/(context['request']['id']+'_'+str(item['index'])+'_'+item['phase']+'.json'),
                                {'request_id':context['request']['id'],'dispatch':dict(item),'events_observed_so_far':list(context['events']),'pending_dispatches':self.pending()})
                except Exception as exc:self.persistence_fault=repr(exc)

    def commit_context(self,context):
        if context['committed'] or context['session'] is None:return
        if not any(d.get('call_attempted') for d in context['dispatches']):return
        if context['session'] is not self.session or self.session_closed:return
        self.session['ground_truth_actions'].append(self.step(context['request']['method'],context['request'].get('args',{}),context['before']))
        self.session['final_state_image']=None;context['committed']=True
        try:self.flush()
        except Exception as exc:
            self.persistence_fault=repr(exc);context['error']='Record persistence failed after dispatch began: '+repr(exc)

    def step(self,method,args,before):
        step=super().step(method,args,before)
        if method=='hotkey' and self.key_policy=='legacy-strict':step['key']=LEGACY_KEYS[args['key'].lower()]
        return step

    def snapshot(self,path):
        if self.session and (self.window.width(),self.window.height())!=self.record_size:raise RuntimeError('Canvas size changed')
        pixmap=self.window.grab()
        if pixmap.isNull():raise RuntimeError('Main Qt window returned null')
        debug=self.run/'debug_frames';debug.mkdir(exist_ok=True)
        raw=debug/(path.stem+'_'+str(time.time_ns())+'_qt_raw.png');save_png(pixmap,raw)
        image=pixmap.toImage().scaled(self.window.size());image.setDevicePixelRatio(1)
        warnings=[];layers=[];gl=[];painter=QtGui.QPainter(image)
        try:
            for number,surface in enumerate(self.surfaces()):
                if not alive(surface):warnings.append({'reason':'deleted_surface'});continue
                origin=surface.mapToGlobal(QtCore.QPoint())-self.window.mapToGlobal(QtCore.QPoint())
                rect=QtCore.QRect(origin,surface.size());cls=surface.metaObject().className()
                if surface is not self.window:
                    entry={'class':cls,'title':surface.windowTitle(),'rect':[origin.x(),origin.y(),surface.width(),surface.height()]}
                    if not self.window.rect().contains(rect):warnings.append({**entry,'reason':'visible_window_outside_or_clipped_by_canvas'})
                    if cls in ('QRollEffect','QAlphaWidget'):
                        warnings.append({**entry,'reason':'decorative_animation_shell_omitted'});continue
                    layer=surface.grab()
                    if layer.isNull():warnings.append({**entry,'reason':'visible_transient_null_pixmap'});continue
                    source=self.run/'layers'/(path.stem+'_'+str(time.time_ns())+'_'+str(number)+'.png');save_png(layer,source)
                    entry.update(image=str(source),sha256=hashlib.sha256(source.read_bytes()).hexdigest());layers.append(entry)
                    logical=layer.toImage().scaled(surface.size());logical.setDevicePixelRatio(1);painter.drawImage(origin,logical)
                if QOpenGLWidget:
                    for widget in surface.findChildren(QOpenGLWidget):
                        if not alive(widget) or not widget.isVisible():continue
                        framebuffer=widget.grabFramebuffer()
                        gl.append({'class':widget.metaObject().className(),'valid':widget.isValid(),'width':widget.width(),'height':widget.height()})
                        if framebuffer.isNull():warnings.append({'class':'QOpenGLWidget','reason':'null_framebuffer'})
        finally:painter.end()
        save_png(image,path)
        return {'image':str(path),'width':image.width(),'height':image.height(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'time':time.time(),'qt_raw_image':str(raw),'layers':layers,'gl_widgets':gl,
                'capture_warnings':warnings,'capture_complete':not warnings}

    def visual_sample(self,path):
        frame=QtGui.QImage(str(path));frame=frame.scaledToWidth(min(640 if np is not None else 320,frame.width())).convertToFormat(QtGui.QImage.Format.Format_RGB32)
        return bytes(frame.constBits()),frame.width(),frame.height(),frame.bytesPerLine()

    def motion(self,current,previous):
        a,w,h,stride=current;b=previous[0]
        if current[1:]!=previous[1:]:return {'quiet':False,'reason':'sample_dimensions_changed'}
        if np is not None:
            aa=np.frombuffer(a,dtype=np.uint8).reshape(h,stride)[:,:w*4].reshape(h,w,4)[:,:,:3]
            bb=np.frombuffer(b,dtype=np.uint8).reshape(h,stride)[:,:w*4].reshape(h,w,4)[:,:,:3]
            changed=np.max(np.abs(aa.astype(np.int16)-bb.astype(np.int16)),axis=2)>12
            rows=(h+31)//32;cols=(w+31)//32
            padded=np.pad(changed,((0,rows*32-h),(0,cols*32-w)))
            counts=padded.reshape(rows,32,cols,32).sum(axis=(1,3))
            sizes=np.outer(np.minimum(32,h-np.arange(rows)*32),np.minimum(32,w-np.arange(cols)*32))
            maximum=float(np.max(counts/sizes));fraction=float(changed.mean());backend='numpy'
        else:
            tiles={};count=0
            for y in range(h):
                for x in range(w):
                    i=y*stride+x*4
                    if max(abs(a[i+j]-b[i+j]) for j in range(3))>12:
                        count+=1;key=(x//32,y//32);tiles[key]=tiles.get(key,0)+1
            maximum=max((n/(min(32,w-tx*32)*min(32,h-ty*32)) for (tx,ty),n in tiles.items()),default=0)
            fraction=count/(w*h);backend='python-320px-fallback'
        return {'changed_pixel_fraction':fraction,'max_tile_changed_fraction':maximum,'quiet':maximum<=0.012,
                'sample_width':w,'pixel_delta_threshold':12,'tile_size':32,'backend':backend}

    def validate_args(self,method,args):
        def point(values):
            if len(values)!=2 or any(type(v) is not int for v in values):raise ValueError('Coordinates must be integers')
            x,y=values
            if not (0<=x<self.window.width() and 0<=y<self.window.height()):raise ValueError('Outside canvas')
        if method in ('click','right_click','double_click','scroll'):point([args['x'],args['y']])
        if method in ('click','double_click','drag'):
            button=args.get('button','left')
            if button not in ('left','middle','right'):raise ValueError('Unknown mouse button')
            if method in ('click','double_click') and button=='right':raise ValueError('Use right_click explicitly')
            if method=='double_click' and button!='left':raise ValueError('Only left double-click is supported')
            self.modifiers(args.get('modifier',''))
        if method=='right_click':
            if args.get('button','right')!='right':raise ValueError('Conflicting right_click button')
            self.modifiers(args.get('modifier',''))
        if method=='drag':point(args['start']);point(args['end'])
        if method=='scroll' and (args.get('direction') not in ('up','down') or type(args.get('amount',1)) is not int or args.get('amount',1)<1):raise ValueError('Invalid scroll direction/amount')
        if method in ('type','type_and_enter') and (not args.get('text') or any(ord(c)<32 or ord(c)>126 for c in args['text'])):raise ValueError('Nonempty printable ASCII text required')
        if method=='hotkey':
            key=args['key'];seq=QtGui.QKeySequence({'esc':'Escape','enter':'Return'}.get(key.lower(),key))
            if seq.isEmpty() or seq.count()!=1 or seq[0].key()==Qt.Key.Key_unknown:raise ValueError('Multi-chord, unknown or empty hotkey unsupported')
            if self.key_policy=='legacy-strict' and key.lower() not in LEGACY_KEYS:raise ValueError('Hotkey outside legacy vocabulary; no GUI input')

    def prepare_action(self,request,probe=None):
        probe=probe or {'started':time.monotonic(),'count':0}
        try:
            path=self.run/'observations'/(request['id']+'_preprobe_%03d.png'%probe['count']);probe['count']+=1
            before=self.snapshot(path)
            if not before['capture_complete']:raise RuntimeError('Incomplete before capture; no GUI input dispatched')
            sample=self.visual_sample(path);old=probe.get('sample');metric=self.motion(sample,old) if old else {'quiet':False};probe['sample']=sample
            now=time.monotonic()
            if metric['quiet']:probe.setdefault('quiet_since',now)
            else:probe.pop('quiet_since',None)
            if now-probe.get('quiet_since',now)<0.6:
                if now-probe['started']<4:QtCore.QTimer.singleShot(150,lambda:self.prepare_action(request,probe));return
                raise RuntimeError('Before image not visually quiet; no GUI input dispatched')
            if self.session:
                dest=self.record_dir/'raw_images'/('step_%03d.png'%len(self.session['ground_truth_actions']))
                tmp=dest.with_suffix('.png.tmp');tmp.write_bytes(path.read_bytes());os.replace(tmp,dest);before['image']=str(dest)
            context={'request':request,'before':before,'session':self.session,'events':[],'dispatches':[],'outbound':[],
                     'committed':False,'error':None,'started':time.monotonic(),'response_sent':False}
            self.contexts[request['id']]=context;self.dispatch_stack.append(context)
            try:self.apply(request['method'],request.get('args',{}))
            except Exception as exc:context['error']=repr(exc);context['cancel_remaining']=True
            finally:self.dispatch_stack.pop()
            # Persist complete immutable plan before any event is posted.
            atomic_json(self.run/'dispatch_journal'/(request['id']+'.json'),{'request_id':request['id'],'planned_dispatches':[dict(d) for d in context['dispatches']],
                        'scope':'Immutable plan; lifecycle files record actual queue/entry/return'})
            for envelope in context['outbound']:
                item=envelope.item
                if context.get('cancel_remaining'):
                    item['phase']='cancelled_not_dispatched';self.lifecycle(context,item);continue
                try:
                    self.app.postEvent(self.router,envelope);item.update(phase='queued',queued_at=time.time());self.lifecycle(context,item)
                except Exception as exc:context['error']=repr(exc);context['cancel_remaining']=True
            QtCore.QTimer.singleShot(350,lambda:self.finish_action(context))
        except Exception as exc:
            # Plans have not been posted if the plan-write failed; cancel them.
            context=self.contexts.get(request['id'])
            if context:
                context['cancel_remaining']=True
                for item in context['dispatches']:
                    if item['phase']=='planned':item['phase']='cancelled_not_dispatched'
            frame=locals().get('before')
            if frame and self.session and Path(frame['image']).parent==self.record_dir/'raw_images':
                dest=self.run/'observations'/(request['id']+'_rejected_prepared_before.png')
                try:Path(frame['image']).replace(dest);frame['image']=str(dest)
                except Exception:pass
            self.respond(request,{'ok':False,'error':repr(exc),'input_may_have_occurred':False,
                                 'before':frame,'traceback':traceback.format_exc()})

    def prepare_stop(self,path,request,probe=None):
        probe=probe or {'started':time.monotonic(),'count':0}
        try:
            shot=self.run/'observations'/(request['id']+'_stopprobe_%03d.png'%probe['count']);probe['count']+=1
            frame=self.snapshot(shot)
            if not frame['capture_complete']:raise RuntimeError('Final capture incomplete; use partial stop')
            sample=self.visual_sample(shot);previous=probe.get('sample');metric=self.motion(sample,previous) if previous else {'quiet':False};probe['sample']=sample
            now=time.monotonic()
            if metric['quiet']:probe.setdefault('quiet_since',now)
            else:probe.pop('quiet_since',None)
            if now-probe.get('quiet_since',now)<0.6:
                if now-probe['started']<4:QtCore.QTimer.singleShot(150,lambda:self.prepare_stop(path,request,probe));return
                raise RuntimeError('Final image not visually quiet; normal stop refused')
            self.stop_quiet_tokens.add(request['id']);self.handle_request(path)
        except Exception as exc:self.respond(request,{'ok':False,'error':repr(exc),'input_may_have_occurred':False,'record_still_active':self.session is not None})

    def poll(self):
        if self.busy:return
        for path in sorted((self.run/'requests').glob('*.json')):
            if path.stem in self.seen_requests or (self.run/'responses'/path.name).exists():continue
            self.seen_requests.add(path.stem);self.busy=True
            QtCore.QTimer.singleShot(0,lambda p=path:self.handle_request(p));return

    def respond(self,request,response):
        try:atomic_json(self.run/'responses'/(request['id']+'.json'),response)
        except Exception as exc:
            self.persistence_fault='Response persistence failed; request will not be replayed: '+repr(exc)
        finally:self.current_id=None;self.busy=False

    def finish_action(self,context):
        request=context['request'];before=context['before'];after=None
        self.commit_context(context)
        try:
            if any(d['phase']=='queued' for d in context['dispatches']):
                if time.monotonic()-context['started']<5:QtCore.QTimer.singleShot(50,lambda:self.finish_action(context));return
                context['error']='Input queue did not enter; no blind retry'
            if not context['committed'] and context['session'] is not None and not context.get('before_moved'):
                target=self.run/'observations'/(request['id']+'_undispatched_before.png');Path(before['image']).replace(target);before['image']=str(target);context['before_moved']=True
            after=self.snapshot(self.run/'observations'/(request['id']+'_after.png'))
            sample=self.visual_sample(after['image']);previous=context.get('last_sample');metric=self.motion(sample,previous) if previous else {'quiet':False}
            context['last_sample']=sample
            now=time.monotonic()
            if metric['quiet']:context.setdefault('quiet_since',now)
            else:context.pop('quiet_since',None)
            quiet_duration=now-context.get('quiet_since',now)
            if not context['error'] and after['capture_complete'] and quiet_duration<0.6 and now-context['started']<4:
                QtCore.QTimer.singleShot(150,lambda:self.finish_action(context));return
            pending=[dict(d) for d in context['dispatches'] if d['phase']=='entered']
            stable=bool(metric['quiet'] and quiet_duration>=0.6)
            response={'ok':context['error'] is None and after['capture_complete'],'before':before,'after':after,
                      'events':list(context['events']),'dispatch_attempts':[dict(d) for d in context['dispatches']],
                      'error':context['error'],'input_may_have_occurred':any(d.get('call_attempted') for d in context['dispatches']),
                      'delivery_sequence_entered':not any(d['phase']=='queued' for d in context['dispatches']),
                      'handler_completion_complete':not pending,'pending_handlers':pending,'ready_for_next_request':stable and context['error'] is None and after['capture_complete'],
                      'stability':{'verified':stable,'visual_quiet':stable,'quiet_seconds':quiet_duration,'metrics':metric,
                                   'scope':'640px image tile-change criterion only; not GUI semantic/camera completion'},
                      'semantic_completion_verified':False,'isolation':isolation()}
            if context['session'] is self.session and self.session is not None and not self.session_closed:
                self.session['final_state_image']=after['image']
                if context['error'] or not after['capture_complete'] or not stable:
                    self.quality_issues.append({'request_id':request['id'],'committed_step':len(self.session['ground_truth_actions']),
                        'error':context['error'],'capture_warnings':after['capture_warnings'],'visual_quiet':stable})
                self.flush()
        except Exception as exc:
            response={'ok':False,'before':before,'after':after,'error':repr(exc),'events':list(context['events']),
                      'dispatch_attempts':[dict(d) for d in context['dispatches']],'input_may_have_occurred':any(d.get('call_attempted') for d in context['dispatches']),'traceback':traceback.format_exc()}
            if context['session'] is self.session and self.session is not None and not self.session_closed:
                self.session['final_state_image']=None;self.quality_issues.append({'request_id':request['id'],'capture_failed':repr(exc)})
                try:self.flush()
                except Exception as io_error:self.persistence_fault=repr(io_error)
        context['response_sent']=True
        try:atomic_json(self.run/'raw_events'/(request['id']+'.json'),{'request':request,'response':response})
        except Exception as exc:
            self.persistence_fault=repr(exc);response={'ok':False,'error':'Evidence persistence failed: '+repr(exc),'input_may_have_occurred':any(d.get('call_attempted') for d in context['dispatches'])}
        self.respond(request,response)
        context.pop('last_sample',None);context['outbound']=[]
        if len(self.contexts)>256:
            for rid,old in list(self.contexts.items()):
                if len(self.contexts)<=256:break
                if old.get('response_sent') and not any(d['phase'] in ('queued','entered') for d in old['dispatches']):self.contexts.pop(rid,None)

    def handle_request(self,path):
        request={'id':path.stem}
        try:
            request=json.loads(path.read_text(encoding='utf-8'));self.current_id=request['id']
            if not isolation()['isolated']:raise RuntimeError('Refusing input on active desktop')
            method=request['method'];args=request.get('args',{})
            if self.persistence_fault and method not in ('observe','stop_record_partial'):raise RuntimeError('Persistence fault; refusing further input: '+self.persistence_fault)
            if method=='observe':
                frame=self.snapshot(self.run/'observations'/(request['id']+'.png'))
                self.respond(request,{'ok':True,'frame':frame,'tree':self.tree(),'info':self.info()});return
            if method=='start_record':
                if self.session or self.pending():raise RuntimeError('Active session or pending delivery; cannot start another record')
                name=args.get('name','')
                if not name.replace('_','').replace('-','').isalnum():raise ValueError('Invalid record name')
                policy=args.get('key_policy','legacy-strict')
                if policy not in ('legacy-strict','qt-rich-not-legacy'):raise ValueError('Unknown key policy')
                directory=self.run/'records'/name;directory.mkdir(parents=True,exist_ok=False);(directory/'raw_images').mkdir()
                # Transactional start: screenshot must succeed before session activation.
                initial=self.snapshot(directory/'initial_aux.png')
                if not initial['capture_complete']:raise RuntimeError('Initial capture incomplete; recording not activated')
                self.record_dir=directory;self.key_policy=policy;self.record_size=(self.window.width(),self.window.height());self.task=args.get('task','NEUTRAL')
                self.session={'task_id':'FREECAD_RECORD_000','task_category':'FreeCAD GUI Agent Training','total_steps':0,'final_state_image':None,'ground_truth_actions':[]}
                self.session_closed=False;self.quality_issues=[];self.record_state='active'
                try:self.flush()
                except Exception:
                    self.session=None;self.session_closed=True;self.record_state='start_failed';raise
                self.respond(request,{'ok':True,'record_dir':str(directory),'initial':initial,'key_policy':policy});return
            if method in ('stop_record','stop_record_partial'):
                if not self.session:raise RuntimeError('No recording active')
                pending=self.pending();final=None;error=None
                if pending and method=='stop_record':raise RuntimeError('Pending handlers: close modal/menu before normal stop, or explicitly use partial stop')
                if method=='stop_record' and request['id'] not in self.stop_quiet_tokens:
                    self.prepare_stop(path,request);return
                self.stop_quiet_tokens.discard(request['id'])
                try:
                    final=self.snapshot(self.record_dir/'raw_images'/('step_%03d.png'%len(self.session['ground_truth_actions'])))
                    if not final['capture_complete']:error='Final capture incomplete'
                except Exception as exc:error=repr(exc)
                if pending:error='Pending handler completion; interrupted record'
                if error and method=='stop_record':raise RuntimeError(error)
                self.record_state='interrupted' if error else 'stopped';self.session['final_state_image']=None if error else final['image']
                if error:self.quality_issues.append({'stop_error':error,'pending_dispatches':pending})
                persistence_error=None
                try:self.flush()
                except Exception as exc:
                    persistence_error=repr(exc);self.persistence_fault=persistence_error;error=error or 'Final record persistence failed';self.record_state='interrupted';self.session['final_state_image']=None
                    if method=='stop_record':raise
                directory=self.record_dir;steps=len(self.session['ground_truth_actions']);self.session_closed=True;self.session=None
                self.respond(request,{'ok':True,'complete':error is None,'steps':steps,'record_dir':str(directory),'record_state':self.record_state,'final':final,'capture_error':error,'persistence_error':persistence_error,'record_persisted':persistence_error is None});return
            if method not in ('click','right_click','double_click','drag','scroll','type','type_and_enter','hotkey'):raise ValueError('Method not whitelisted')
            self.validate_args(method,args)
            self.prepare_action(request)
        except Exception as exc:self.respond(request,{'ok':False,'error':repr(exc),'input_may_have_occurred':False,'traceback':traceback.format_exc()})


def start(window=None):
    if window is None:
        import FreeCADGui
        window=FreeCADGui.getMainWindow()
        # Full-screen only inside this process's non-input desktop. Qt pop-ups
        # are then screen-bounded inside the same captured GUI canvas.
        window.showFullScreen()
    return QueuedBridge(window)
