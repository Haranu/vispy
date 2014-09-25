""" Implementation to execute GL Intermediate Representation (GLIR)

"""

import sys
import re

import numpy as np

from . import gl
from ..util import logger


class GlirQueue(object):
    """ Representation of a queue of GLIR commands. One instance of
    this class is attached to each context object, and gloo will post
    commands to this queue.
    
    Upon drawing (i.e. `Program.draw()`) The commands in the queue are
    pushed to an interpreter. This can be Python, JS, whatever. For
    now, each queue has a GlirParser object that does the interpretation
    in Python directly. This should later be replaced by some sort of
    plugin mechanism.
    """
    
    def __init__(self):
        self._commands = []
        self._parser = GlirParser()
        self._invalid_objects = set()
        # todo: allow different kind of parsers, like a parser that sends to JS
    
    def command(self, *args):
        """ Send a command. See the command spec at:
        https://github.com/vispy/vispy/wiki/Spec.-Gloo-IR
        """
        self._commands.append(args)
        if args[0] == 'CREATE' and args[-1] is None:
            self._invalid_objects.add(args[1])
    
    def show(self):
        """ Print the list of commands currently in the queue.
        """
        for command in self._commands:
            if command[0] is None or command[1] in self._invalid_objects:
                continue  # Skip nill commands 
            t = []
            for e in command:
                if isinstance(e, np.ndarray):
                    t.append('array %s' % str(e.shape))
                elif isinstance(e, str):
                    s = e.strip()
                    if len(s) > 20:
                        s = s[:18] + '... %i lines' % (e.count('\n')+1)
                    t.append(s)
                else:
                    t.append(e)
            print(tuple(t))
    
    def clear(self):
        """ Pop the whole queue and return it as a list.
        """
        self._commands, ret = [], self._commands
        return ret
        
    def parse(self):
        """ Interpret all commands; do the OpenGL calls.
        """
        self._parser.parse(self.clear())


class GlirParser(object):
    """ A class for interpreting GLIR commands
    
    We make use of relatively light GLIR objects that are instantiated
    on CREATE commands. These objects are stored by their id in a
    dictionary so that commands like ACTIVATE and DATA can easily
    be executed on the corresponding objects.
    """
    
    def __init__(self):
        self._objects = {}
        self._invalid_objects = set()
        self._classmap = {'VERTEXBUFFER': GlirVertexBuffer,
                          'INDEXBUFFER': GlirIndexBuffer,
                          'PROGRAM': GlirProgram,
                          'Texture2D': GlirTexture2D,
                          'Texture3D': GlirTexture3D,
                          
                          }
    
    def get_object(self, id):
        """ Get the object with the given id or None if it does not exist.
        """
        return self._objects.get(id, None)
    
    def parse(self, commands):
        """ Parse a list of commands.
        """
        
        for command in commands:
            cmd, id, args = command[0], command[1], command[2:]
            
            if cmd == 'CREATE':
                # Creating an object
                if args[0] is not None:
                    klass = self._classmap[args[0]]
                    self._objects[id] = klass(self)
                else:
                    self._invalid_objects.add(id)
            elif cmd == 'DELETE':
                # Deleteing an object
                ob = self._objects.get(id, None)
                if ob is not None:
                    ob.delete()
            else:
                # Doing somthing to an object
                ob = self._objects.get(id, None)
                if ob is None:
                    if id not in self._invalid_objects:
                        print('Cannot %s object %i because it does not exist' %
                              (cmd, id))
                    continue
                #
                if cmd == 'ACTIVATE':
                    ob.activate()
                elif cmd == 'DEACTIVATE':
                    ob.deactivate()
                elif cmd == 'SIZE':
                    ob.set_size(*args)
                elif cmd == 'DATA':
                    ob.set_data(*args)
                elif cmd == 'SHADERS':
                    ob.set_shaders(*args)
                elif cmd == 'UNIFORM':
                    ob.set_uniform(*args)
                elif cmd == 'ATTRIBUTE':
                    ob.set_attribute(*args)
                elif cmd == 'DRAW':
                    ob.draw(*args)
                elif cmd == 'SET':
                    getattr(ob, 'set_'+args[0])(*args[1:])
                else:
                    print('Invalud GLIR command %r' % cmd)


## GLIR objects


class GlirObject(object):
    @property
    def handle(self):
        return self._handle


class GlirProgram(GlirObject):
    
    UTYPEMAP = {
        'float': 'glUniform1fv',
        'vec2': 'glUniform2fv',
        'vec3': 'glUniform3fv',
        'vec4': 'glUniform4fv',
        'int': 'glUniform1iv',
        'ivec2': 'glUniform2iv',
        'ivec3': 'glUniform3iv',
        'ivec4': 'glUniform4iv',
        'bool': 'glUniform1iv',
        'bvec2': 'glUniform2iv',
        'bvec3': 'glUniform3iv',
        'bvec4': 'glUniform4iv',
        'mat2': 'glUniformMatrix2fv',
        'mat3': 'glUniformMatrix3fv',
        'mat4': 'glUniformMatrix4fv',
        'sampler2D': 'glUniform1i',
        'sampler3D': 'glUniform1i',
    }
    
    ATYPEMAP = {
        'float': 'glVertexAttrib1f',
        'vec2': 'glVertexAttrib2f',
        'vec3': 'glVertexAttrib3f',
        'vec4': 'glVertexAttrib4f',
    }
    
    ATYPEINFO = {
        'float': (1, gl.GL_FLOAT, np.float32),
        'vec2': (2, gl.GL_FLOAT, np.float32),
        'vec3': (3, gl.GL_FLOAT, np.float32),
        'vec4': (4, gl.GL_FLOAT, np.float32),
    }
    
    def __init__(self, parser):
        self._parser = parser
        self._parser._current_program = 0 
        self._handle = gl.glCreateProgram()
        self._validated = False
        # Keeping track of uniforms/attributes
        self._handles = {}  # cache with handles to attributes/uniforms
        self._unset_variables = set()
        # Store samplers in buffers that are bount to uniforms/attributes
        # todo: store these by id?
        self._samplers = {}  # name -> (unit, GlirTexture)
        self._buffers = {}  # name -> GlirBuffer
    
    def delete(self):
        gl.glDeleteProgram(self._handle)
    
    def use_this_program(self):
        """ Avoid overhead in calling glUseProgram with same arg.
        Warning: this will break if glUseProgram is used somewhere else.
        Per context we keep track of one current program.
        """
        if id != self._parser._current_program:
            self._parser._current_program = id
            gl.glUseProgram(self._handle)
    
    def activate(self):
        self.use_this_program()
        # Activate textures
        for tex, unit in self._samplers.values():
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
            tex.activate()
        # Activate buffers
        for vbo in self._buffers.values():
            vbo.activate()
        # Validate. We need to validate after textures units get assigned
        if not self._validated:
            self._validated = True
            # Validate ourselves
            if self._unset_variables:
                logger.warn('Program has unset variables: %r' % 
                            self._unset_variables)
            # Validate via OpenGL
            gl.glValidateProgram(self._handle)
            if not gl.glGetProgramParameter(self._handle, 
                                            gl.GL_VALIDATE_STATUS):
                print(gl.glGetProgramInfoLog(self._handle))
                raise RuntimeError('Program validation error')
    
    def deactivate(self):
        # No need to deactivate each texture/buffer!
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        # todo: deactivate texture
        # No need to deactivate this program, AFAIK there is no situation
        # where current program should be 0.
    
    def set_shaders(self, vert, frag):
        """ This function takes care of setting the shading code and
        compiling+linking it into a working program object that is ready
        to use.
        """
        # Create temporary shader objects
        vert_handle = gl.glCreateShader(gl.GL_VERTEX_SHADER)
        frag_handle = gl.glCreateShader(gl.GL_FRAGMENT_SHADER)
        # For both vertex and fragment shader: set source, compile, check
        for code, handle, type in [(vert, vert_handle, 'vertex'), 
                                   (frag, frag_handle, 'fragment')]:
            gl.glShaderSource(handle, code)
            gl.glCompileShader(handle)
            status = gl.glGetShaderParameter(handle, gl.GL_COMPILE_STATUS)
            if not status:
                errors = gl.glGetShaderInfoLog(handle)
                errormsg = self._get_error(code, errors, 4)
                raise RuntimeError("Shader compilation error in %s:\n%s" % 
                                   (type + ' shader', errormsg))
        # Attach shaders
        gl.glAttachShader(self._handle, vert_handle)
        gl.glAttachShader(self._handle, frag_handle)
        # Link the program and check
        gl.glLinkProgram(self._handle)
        if not gl.glGetProgramParameter(self._handle, gl.GL_LINK_STATUS):
            print(gl.glGetProgramInfoLog(self._handle))
            raise RuntimeError('Program linking error')
        # Now we can remove the shaders. We no longer need them and it
        # frees up precious GPU memory:
        # http://gamedev.stackexchange.com/questions/47910
        gl.glDetachShader(self._handle, vert_handle)
        gl.glDetachShader(self._handle, frag_handle)
        gl.glDeleteShader(vert_handle)
        gl.glDeleteShader(frag_handle)
        # Now we know what variables will be used by the program
        self._unset_variables = self._get_active_attributes_and_uniforms()
        self._handles = {}
        
    def _get_active_attributes_and_uniforms(self):
        """ Retrieve active attributes and uniforms to be able to check that
        all uniforms/attributes are set by the user.
        """
        # This match a name of the form "name[size]" (= array)
        regex = re.compile("""(?P<name>\w+)\s*(\[(?P<size>\d+)\])\s*""")
        # Get how many active attributes and uniforms there are
        cu = gl.glGetProgramParameter(self._handle, gl.GL_ACTIVE_UNIFORMS)
        ca = gl.glGetProgramParameter(self.handle, gl.GL_ACTIVE_ATTRIBUTES)
        # Get info on each one
        attributes = []
        uniforms = []
        for container, count, func in [(attributes, ca, gl.glGetActiveAttrib),
                                       (uniforms, cu, gl.glGetActiveUniform)]:
            for i in range(count):
                name, size, gtype = func(self._handle, i)
                m = regex.match(name)  # Check if xxx[0] instead of xx
                if m:
                    name = m.group('name')
                    for i in range(size):
                        container.append(('%s[%d]' % (name, i), gtype))
                else:
                    container.append((name, gtype))
        #return attributes, uniforms
        return set([v[0] for v in attributes] + [v[0] for v in uniforms])
    
    def _parse_error(self, error):
        """ Parses a single GLSL error and extracts the linenr and description
        """
        error = str(error)
        # Nvidia
        # 0(7): error C1008: undefined variable "MV"
        m = re.match(r'(\d+)\((\d+)\)\s*:\s(.*)', error)
        if m:
            return int(m.group(2)), m.group(3)
        # ATI / Intel
        # ERROR: 0:131: '{' : syntax error parse error
        m = re.match(r'ERROR:\s(\d+):(\d+):\s(.*)', error)
        if m:
            return int(m.group(2)), m.group(3)
        # Nouveau
        # 0:28(16): error: syntax error, unexpected ')', expecting '('
        m = re.match(r'(\d+):(\d+)\((\d+)\):\s(.*)', error)
        if m:
            return int(m.group(2)), m.group(4)
        # Other ...
        return None, error

    def _get_error(self, code, errors, indentation=0):
        """Get error and show the faulty line + some context
        """
        # Init
        results = []
        lines = None
        if code is not None:
            lines = [line.strip() for line in code.split('\n')]

        for error in errors.split('\n'):
            # Strip; skip empy lines
            error = error.strip()
            if not error:
                continue
            # Separate line number from description (if we can)
            linenr, error = self._parse_error(error)
            if None in (linenr, lines):
                results.append('%s' % error)
            else:
                results.append('on line %i: %s' % (linenr, error))
                if linenr > 0 and linenr < len(lines):
                    results.append('  %s' % lines[linenr - 1])

        # Add indentation and return
        results = [' ' * indentation + r for r in results]
        return '\n'.join(results)
    
    def set_uniform(self, name, type, value):
        """ Set a uniform value. Value is assumed to have been checked.
        """
        # Get handle for the uniform, first try cache
        handle = self._handles.get(name, -1)
        if handle < 0:
            handle = gl.glGetUniformLocation(self._handle, name)
            self._unset_variables.discard(name)  # Mark as set
            self._handles[name] = handle  # Store in cache
            if handle < 0:
                logger.warn('Variable %s is not an active uniform' % name)
                return
        # Look up function to call
        funcname = self.UTYPEMAP[type]
        func = getattr(gl, funcname)
        # Program needs to be active in order to set uniforms
        self.use_this_program()
        # Triage depending on type 
        if type.startswith('mat'):
            # Value is matrix, these gl funcs have alternative signature
            transpose = False  # OpenGL ES 2.0 does not support transpose
            func(handle, 1, transpose, value)
        elif type.startswith('sampler'):
            # Sampler: the value is the id of the texture
            tex = self._parser.get_object(value)
            if tex is None:
                raise RuntimeError('Could not find texture with id %i' % value)
            self._samplers.pop(name, None)  # First remove possibly old version
            unit = self._get_free_unit([s[1] for s in self._samplers.values()])
            self._samplers[name] = tex, unit
            gl.glUniform1i(handle, unit)
        else:
            # Regular uniform
            func(handle, 1, value)
    
    def set_attribute(self, name, type, value):
        """ Set an attribute value. Value is assumed to have been checked.
        """
        # Get handle for the attribute, first try cache
        handle = self._handles.get(name, -1)
        if handle < 0:
            handle = gl.glGetAttribLocation(self._handle, name)
            self._unset_variables.discard(name)  # Mark as set
            self._handles[name] = handle  # Store in cache
            if handle < 0:
                logger.warn('Variable %s is not an active attribute' % name)
                return
        # Program needs to be active in order to set uniforms
        self.use_this_program()
        # Triage depending on VBO or tuple data
        if value[0] == 0:
            # Look up function call
            funcname = self.ATYPEMAP[type]
            func = getattr(gl, funcname)
            # Set data
            gl.glDisableVertexAttribArray(handle)
            func(handle, *value[1:])
            self._buffers.pop(name, None)
        else:
            # Get meta data
            vbo_id, stride, offset = value
            size, gtype, dtype = self.ATYPEINFO[type]
            # Get associated VBO
            vbo = self._parser.get_object(vbo_id)
            if vbo is None:
                raise RuntimeError('Could not find VBO with id %i' % vbo_id)
            # Set data
            vbo.activate()
            gl.glEnableVertexAttribArray(handle)
            gl.glVertexAttribPointer(
                handle, size, gtype, gl.GL_FALSE, stride, offset)
            # Store
            self._buffers[name] = vbo
    
    def _get_free_unit(self, units):
        """ Get free number given a list of numbers. For texture unit.
        """
        if not units:
            return 1
        min_unit, max_unit = min(units), max(units)
        if min_unit > 1:
            return min_unit - 1
        elif len(units) < (max_unit - min_unit + 1):
            return set(range(min_unit+1, max_unit)).difference(units).pop()
        else:
            return max_unit + 1
    
    def draw(self, mode, selection):
        """ Draw program in given mode, with given selection (IndexBuffer or
        first, count).
        """
        # Init
        self.activate()
        gl.check_error('Check before draw')
        # Draw
        if len(selection) == 3:
            id, gtype, count = selection
            ibuf = self._parser.get_object(id)
            ibuf.activate()
            gl.glDrawElements(mode, count, gtype, None)
            ibuf.deactivate()
        else:
            first, count = selection
            gl.glDrawArrays(mode, first, count)
        # Wrap up
        gl.check_error('Check after draw')
        self.deactivate()


class GlirBuffer(GlirObject):
    _target = None
    _usage = gl.GL_DYNAMIC_DRAW  # STATIC_DRAW, STREAM_DRAW or DYNAMIC_DRAW
    
    def __init__(self, parser):
        self._handle = gl.glCreateBuffer()
        self._buffer_size = 0
        self._bufferSubDataOk = False
    
    def delete(self):
        gl.glDeleteBuffer(self._handle)
    
    def activate(self):
        gl.glBindBuffer(self._target, self._handle)
    
    def deactivate(self):
        gl.glBindBuffer(self._target, 0)
    
    def set_size(self, nbytes):  # in bytes
        if nbytes != self._buffer_size:
            gl.glBindBuffer(self._target, self._handle)
            gl.glBufferData(self._target, nbytes, self._usage)
            self._buffer_size = nbytes
    
    def set_data(self, offset, data):
        gl.glBindBuffer(self._target, self._handle)
        
        nbytes = data.nbytes
        
        # Determine whether to check errors to try handling the ATI bug
        check_ati_bug = ((not self._bufferSubDataOk) and
                         (gl.current_backend is gl.desktop) and
                         sys.platform.startswith('win'))

        # flush any pending errors
        if check_ati_bug:
            gl.check_error('periodic check')
        
        try:
            gl.glBufferSubData(self._target, offset, data)
            if check_ati_bug:
                gl.check_error('glBufferSubData')
            self._bufferSubDataOk = True  # glBufferSubData seems to work
        except Exception:
            # This might be due to a driver error (seen on ATI), issue #64.
            # We try to detect this, and if we can use glBufferData instead
            if offset == 0 and nbytes == self._buffer_size:
                gl.glBufferData(self._target, data, self._usage)
                logger.debug("Using glBufferData instead of " +
                             "glBufferSubData (known ATI bug).")
            else:
                raise
    
    def exec_commands(self, commands):
        
        # todo: move this in the queue, before sending it of to a parser
        # Efficiency: purge all data commands that are followed by a resize
        data_commands = []
        other_commands = []
        for command in commands:
            if command[0] == 'resize':
                data_commands = []
            if command[0] == 'data':
                data_commands.append(command)
            else:
                other_commands.append(command)
        commands = other_commands + data_commands


class GlirVertexBuffer(GlirBuffer):
    _target = gl.GL_ARRAY_BUFFER
    

class GlirIndexBuffer(GlirBuffer):
    _target = gl.GL_ELEMENT_ARRAY_BUFFER


class GlirTexture(GlirObject):
    _target = None
    
    _types = {
        np.dtype(np.int8): gl.GL_BYTE,
        np.dtype(np.uint8): gl.GL_UNSIGNED_BYTE,
        np.dtype(np.int16): gl.GL_SHORT,
        np.dtype(np.uint16): gl.GL_UNSIGNED_SHORT,
        np.dtype(np.int32): gl.GL_INT,
        np.dtype(np.uint32): gl.GL_UNSIGNED_INT,
        # np.dtype(np.float16) : gl.GL_HALF_FLOAT,
        np.dtype(np.float32): gl.GL_FLOAT,
        # np.dtype(np.float64) : gl.GL_DOUBLE
    }
    
    def __init__(self, parser):
        self._handle = gl.glCreateTexture()
        self._shape_format = 0
    
    def delete(self):
        gl.glDeleteTexture(self._handle)
    
    def activate(self):
        # todo: NO NEED FOR AN ACTIVATE COMMAND, EXCEPT FBO!
        gl.glBindTexture(self._target, self._handle)
    
    def deactivate(self):
        gl.glBindTexture(self._target, 0)
    
    # Taken from pygly
    def _get_alignment(self, width):
        """Determines a textures byte alignment.

        If the width isn't a power of 2
        we need to adjust the byte alignment of the image.
        The image height is unimportant

        www.opengl.org/wiki/Common_Mistakes#Texture_upload_and_pixel_reads
        """
        # we know the alignment is appropriate
        # if we can divide the width by the
        # alignment cleanly
        # valid alignments are 1,2,4 and 8
        # put 4 first, since it's the default
        alignments = [4, 8, 2, 1]
        for alignment in alignments:
            if width % alignment == 0:
                return alignment
    
    # todo: we could also do (SET id DATA) for setting data :/
    def set_wrapping(self, wrapping):
        self.activate()
        gl.glTexParameterf(self._target, gl.GL_TEXTURE_WRAP_S,
                           wrapping[0])
        gl.glTexParameterf(self._target, gl.GL_TEXTURE_WRAP_T,
                           wrapping[1])
    
    def set_interpolation(self, interpolation):
        self.activate()
        gl.glTexParameterf(self._target, gl.GL_TEXTURE_MIN_FILTER,
                           interpolation[0])
        gl.glTexParameterf(self._target, gl.GL_TEXTURE_MAG_FILTER,
                           interpolation[1])


class GlirTexture2D(GlirTexture):
    _target = gl.GL_TEXTURE_2D
    
    def set_size(self, shape, format):
        # Shape is height, width
        if (shape, format) != self._shape_format:
            self._format = format
            gl.glTexImage2D(self._target, 0, self._format, self._format,
                            gl.GL_BYTE, shape[:2])
    
    def set_data(self, offset, data):
        y, x = offset
        # Get gtype
        gtype = self._types.get(np.dtype(data.dtype), None)
        print(gtype, self._format)
        if gtype is None:
            raise ValueError("Type %r not allowed for texture" % data.dtype)
        # Set alignment (width is nbytes_per_pixel * npixels_per_line)
        alignment = self._get_alignment(data.shape[-2]*data.shape[-1])
        if alignment != 4:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, alignment)
        # Upload
        gl.glTexSubImage2D(self._target, 0, x, y, self._format,
                           gtype, data)
        # Set alignment back
        if alignment != 4:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)


GL_SAMPLER_3D = gl.Enum('GL_SAMPLER_3D', 35679)
GL_TEXTURE_3D = gl.Enum('GL_TEXTURE_3D', 32879)


def _check_pyopengl_3D():
    """Helper to ensure users have OpenGL for 3D texture support (for now)"""
    try:
        import OpenGL.GL as _gl
    except ImportError:
        raise ImportError('PyOpenGL is required for 3D texture support')
    return _gl


def glTexImage3D(target, level, internalformat, format, type, pixels):
    # Import from PyOpenGL
    _gl = _check_pyopengl_3D()
    border = 0
    assert isinstance(pixels, (tuple, list))  # the only way we use this now
    depth, height, width = pixels
    _gl.glTexImage3D(target, level, internalformat,
                     width, height, depth, border, format, type, None)


def glTexSubImage3D(target, level, xoffset, yoffset, zoffset,
                    format, type, pixels):
    # Import from PyOpenGL
    _gl = _check_pyopengl_3D()
    depth, height, width = pixels.shape[:3]
    _gl.glTexSubImage3D(target, level, xoffset, yoffset, zoffset,
                        width, height, depth, format, type, pixels)


class GlirTexture3D(GlirTexture):
    _target = GL_TEXTURE_3D
        
    def set_size(self, shape, format):
        # Shape is depth, height, width
        if (shape, format) != self._shape_format:
            self._format = format
            glTexImage3D(self._target, 0, self._format, self._format,
                         gl.GL_BYTE, shape[:3])
    
    def set_data(self, offset, data):
        z, y, x = offset
        # Get gtype
        gtype = self._types.get(np.dtype(self.dtype), None)
        if gtype is None:
            raise ValueError("Type not allowed for texture")
        # Set alignment (width is nbytes_per_pixel * npixels_per_line)
        alignment = self._get_alignment(data.shape[-3] *
                                        data.shape[-2] * data.shape[-1])
        if alignment != 4:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, alignment)
        # Upload
        glTexSubImage3D(self._target, 0, x, y, z, self._format,
                        gtype, data)
        # Set alignment back
        if alignment != 4:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
