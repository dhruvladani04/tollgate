# Third-party data in this folder

`injecagent_calls.jsonl` is derived from the
[InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) benchmark
(Zhan et al., 2024), at commit `f19c9f2c79a41046eb13c03c51a24c567a8ffa07`.

- **Taken from InjecAgent as published:** the user instructions, the attacker
  instructions, the tool names and the tool descriptions.
- **Written for Tollgate:** the tool arguments, the labels and the pairing of
  each call with a request. [evals/injecagent.py](../../injecagent.py) holds
  them and builds the file.

InjecAgent is distributed under the MIT licence, reproduced below.

```text
MIT License

Copyright (c) 2023 Qiusi Zhan

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
