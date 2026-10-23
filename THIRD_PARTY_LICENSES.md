# 第三方组件与许可

本工程自带以下第三方文件，均为其原始发布版本，未作修改。

## three.js

- 文件：`web/vendor/three.min.js`
- 版本：见文件头部 `@license` 注释
- 许可：MIT
- 版权：Copyright © 2010-2022 Three.js Authors
- 来源：https://github.com/mrdoob/three.js

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Python 依赖

`requirements.txt` 只依赖 [pyserial](https://github.com/pyserial/pyserial)（BSD-3-Clause）。
不使用 pyserial 时程序会自动回退到内置 termios 实现。
