"""
Note bodies for the demo sandbox (see demo_crud.py).

They are Tiptap HTML, the same format the editor saves, and use only what the
editor supports: headings, lists, tables, code blocks, quotes, highlight,
underline and text alignment. Each starts below the title, which the editor
renders separately from the body.
"""

BST = """
<p>Every node's left subtree holds <mark>smaller keys</mark> and its right subtree
<mark>larger keys</mark>, so a lookup discards half the remaining tree at each step.</p>
<h3>Operations</h3>
<ul>
  <li><strong>Search / insert:</strong> O(h), where h is the tree's height</li>
  <li><strong>Delete:</strong> swap in the in-order successor when a node has two children</li>
  <li><strong>In-order traversal</strong> visits keys in sorted order</li>
</ul>
<h3>Why balance matters</h3>
<p>Inserting sorted data degrades the tree into a linked list (h = n). Self-balancing
variants like <em>AVL</em> and <em>red-black trees</em> keep h = O(log n).</p>
<blockquote><p>Exam tip: know how to trace a left and a right rotation by hand.</p></blockquote>
"""

DYNAMIC_PROGRAMMING = """
<p>DP works when a problem has <mark>overlapping subproblems</mark> and
<mark>optimal substructure</mark>. Write the recurrence first, code second.</p>
<h3>The recipe</h3>
<ol>
  <li>Define the state: what does <code>dp[i]</code> mean, in words?</li>
  <li>Write the recurrence and its base cases</li>
  <li>Pick an order that fills every dependency before it is read</li>
  <li>Recover the answer (and the choices, if asked)</li>
</ol>
<h3>Example: longest increasing subsequence</h3>
<pre><code class="language-python">def lis(nums):
    dp = [1] * len(nums)
    for i in range(len(nums)):
        for j in range(i):
            if nums[j] &lt; nums[i]:
                dp[i] = max(dp[i], dp[j] + 1)
    return max(dp, default=0)</code></pre>
<p>O(n²) as written; patience sorting with binary search gets it to O(n log n).</p>
<blockquote><p>Problem set 6 is all DP. Start with the knapsack question, it's the warm-up.</p></blockquote>
"""

EIGENVALUES = """
<p>For a square matrix A, a nonzero vector v is an <strong>eigenvector</strong> when
<code>Av = λv</code>; the scalar λ is its <strong>eigenvalue</strong>.</p>
<h3>Finding them</h3>
<ol>
  <li>Solve the characteristic equation <code>det(A − λI) = 0</code></li>
  <li>For each λ, find the null space of <code>A − λI</code></li>
</ol>
<h3>Quick facts</h3>
<table>
  <tbody>
    <tr><th><p>Property</p></th><th><p>What it tells you</p></th></tr>
    <tr><td><p>Sum of eigenvalues</p></td><td><p>Equals the trace of A</p></td></tr>
    <tr><td><p>Product of eigenvalues</p></td><td><p>Equals det(A)</p></td></tr>
    <tr><td><p>A triangular</p></td><td><p>Eigenvalues are the diagonal entries</p></td></tr>
    <tr><td><p>n distinct eigenvalues</p></td><td><p>A is diagonalizable</p></td></tr>
  </tbody>
</table>
<p><mark>Quiz 4 covers sections 5.1 to 5.3.</mark></p>
"""

MEMORY = """
<h2>Three stages</h2>
<ul>
  <li><strong>Encoding:</strong> getting information in; deeper processing sticks better</li>
  <li><strong>Storage:</strong> sensory → short-term (about 7 ± 2 items) → long-term</li>
  <li><strong>Retrieval:</strong> recall, recognition and relearning</li>
</ul>
<h2>Why we forget</h2>
<ul>
  <li>Ebbinghaus's forgetting curve: most loss happens in the first day</li>
  <li><em>Proactive</em> interference: old learning blocks new</li>
  <li><em>Retroactive</em> interference: new learning blocks old</li>
</ul>
<blockquote><p>Spaced repetition beats cramming, which is the whole point of the flashcards habit.</p></blockquote>
"""

SPEECH_OUTLINE = """
<p><strong>Topic:</strong> How sleep shapes memory. <strong>Length:</strong> 5 to 6 minutes.</p>
<h3>Introduction</h3>
<ol>
  <li>Attention getter: "You forget half of today's lecture by tomorrow, unless you sleep."</li>
  <li>Thesis and preview of the three main points</li>
</ol>
<h3>Body</h3>
<ol>
  <li>What happens in the brain during deep sleep</li>
  <li>The research: memory consolidation studies</li>
  <li>Practical tips for students</li>
</ol>
<h3>Conclusion</h3>
<p>Restate the thesis, then close by calling back to the opening line.</p>
<p><u>Cite at least three sources</u> and rehearse with a timer twice before Friday.</p>
"""

DRAWING = """
<h3>Value scale</h3>
<p>Nine steps from white to black. Squint to compare values; color fools the eye, value doesn't.</p>
<h3>Shading a sphere</h3>
<ul>
  <li><strong>Highlight:</strong> where the light hits directly</li>
  <li><strong>Core shadow:</strong> the darkest band, where the form turns away</li>
  <li><strong>Reflected light:</strong> a little bounce from the table</li>
  <li><strong>Cast shadow:</strong> darkest right under the object</li>
</ul>
<p>Pencils: 2H for layout, HB for midtones, 4B and 6B for darks.</p>
"""

RETRO = """
<h3>What went well</h3>
<ul>
  <li>Front desk training finished a week early</li>
  <li>New check-out process cut the line at lunch</li>
</ul>
<h3>What to improve</h3>
<ul>
  <li>Shift swaps keep happening over text and getting lost</li>
  <li>Returns cart overflows on Mondays</li>
</ul>
<p><strong>Action item:</strong> put every shift swap on the shared calendar.</p>
"""

INTERNSHIPS = """
<p>Goal: <mark>10 applications by the end of the month</mark>, tailored resume for each.</p>
<table>
  <tbody>
    <tr>
      <th><p>Company</p></th>
      <th><p>Role</p></th>
      <th><p>Status</p></th>
    </tr>
    <tr>
      <td><p>Northwind Labs</p></td>
      <td><p>Software Engineering Intern</p></td>
      <td><p>Phone screen next week</p></td>
    </tr>
    <tr>
      <td><p>Blue Harbor Health</p></td>
      <td><p>Data Analyst Intern</p></td>
      <td><p>Applied</p></td>
    </tr>
    <tr>
      <td><p>Cedar Street Studio</p></td>
      <td><p>Frontend Intern</p></td>
      <td><p>Waiting on referral</p></td>
    </tr>
    <tr>
      <td><p>Pacific Transit Co.</p></td>
      <td><p>Operations Intern</p></td>
      <td><p>Rejected</p></td>
    </tr>
  </tbody>
</table>
<h3>Interview prep</h3>
<ul>
  <li>Two LeetCode mediums a week</li>
  <li>STAR stories: the group project, the library job, the hackathon</li>
</ul>
"""

SEMESTER_GOALS = """
<h2 style="text-align: center">Fall semester goals</h2>
<ol>
  <li><strong>School:</strong> keep a 3.5 GPA and never submit a problem set late</li>
  <li><strong>Health:</strong> gym three times a week, in bed by midnight</li>
  <li><strong>Career:</strong> land a summer internship</li>
  <li><strong>Personal:</strong> read one book a month for fun</li>
</ol>
<hr>
<p style="text-align: center"><em>Small steps every day.</em></p>
"""

WEEKLY_REVIEW = """
<h3>Look back</h3>
<ul>
  <li>What did I finish? What slipped, and why?</li>
  <li>Which habit held up, and which one broke?</li>
</ul>
<h3>Look ahead</h3>
<ul>
  <li>What's due in the next seven days?</li>
  <li>The one thing that would make next week a win</li>
</ul>
<hr>
<p>Sunday evening, 20 minutes, with the calendar open.</p>
"""

HABITS = """
<ul>
  <li><strong>Habit stacking:</strong> attach a new habit to one you already do</li>
  <li><strong>Make it easy:</strong> reduce friction for good habits, add it for bad ones</li>
  <li><strong>Never miss twice:</strong> one missed day is an accident, two is a new pattern</li>
</ul>
<p>Pairs well with the streak tracker: the goal is consistency, not perfection.</p>
"""

RECIPE = """
<p>About 20 minutes, serves two.</p>
<ol>
  <li>Slice chicken thigh thin and toss with soy sauce and cornstarch</li>
  <li>Sear in a very hot pan, then set aside</li>
  <li>Stir fry garlic, ginger and broccoli for 3 minutes</li>
  <li>Return the chicken with a splash of <mark>oyster sauce</mark> and serve over rice</li>
</ol>
"""
